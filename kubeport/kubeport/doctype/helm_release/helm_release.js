frappe.ui.form.on('Helm Release', {
    refresh: function(frm) {
        const status_map = {
            'Deployed': 'green',
            'Failed': 'red',
            'In Progress': 'blue',
            'Uninstalling': 'blue',
            'Degraded': 'yellow',
            'Draft': 'orange'
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
        }
        kubeport_configure_release_actions(frm);

        if (!frm.__helm_release_status_listener_bound) {
            frm.__helm_release_status_listener_bound = true;
            frappe.realtime.on('helm_release_status_update', (data) => {
                if (data.release_docname === frm.doc.name) {
                    frm.reload_doc();
                }
            });
        }

        kubeport_render_release_health(frm);
    },

    chart: function(frm) {
        // When the user selects a chart, auto-populate the chart_version
        // with the latest version and offer to load default values
        if (frm.doc.chart) {
            frappe.db.get_doc('Helm Chart', frm.doc.chart).then(chart_doc => {
                if (chart_doc.latest_version) {
                    frm.set_value('chart_version', chart_doc.latest_version);
                }

                // Offer to load default values if values field is empty
                if (!frm.doc.values && chart_doc.default_values) {
                    frappe.confirm(
                        __('Load default values from the chart?'),
                        () => {
                            frm.set_value('values', chart_doc.default_values);
                            frappe.show_alert({
                                message: __('Default values loaded. Edit as needed.'),
                                indicator: 'green'
                            });
                        }
                    );
                }
            });
        }
    },

    cluster: function(frm) {
        // When the cluster changes, reset namespace to default
        frm.set_value('namespace', 'default');
    },

    load_defaults: function(frm) {
        if (!frm.doc.chart) {
            frappe.throw(__('Select a chart first.'));
            return;
        }

        frappe.call({
            doc: frm.doc,
            method: 'load_defaults',
            freeze: true,
            freeze_message: __('Fetching default values...'),
            callback: function(r) {
                if (r.message) {
                    frm.set_value('values', r.message);
                    frm.dirty();
                    frappe.show_alert({
                        message: __('Default values loaded. Edit as needed, then save.'),
                        indicator: 'green'
                    });
                }
            }
        });
    },

    deploy_release: function(frm) {
        if (frm.is_dirty()) {
            frappe.throw(__('Save the document before deploying.'));
        }

        frappe.confirm(
            __('Install or upgrade release "{0}" to cluster "{1}"?',
                [frm.doc.release_name, frm.doc.cluster]),
            () => {
                frappe.call({
                    doc: frm.doc,
                    method: 'deploy_release',
                    callback: function(r) {
                        if (!r.exc) frm.reload_doc();
                    }
                });
            }
        );
    },

    uninstall_release: function(frm) {
        frappe.confirm(
            __('Uninstall release "{0}" and remove all its resources from the cluster?',
                [frm.doc.release_name]),
            () => {
                frappe.call({
                    doc: frm.doc,
                    method: 'uninstall_release',
                    callback: function(r) {
                        if (!r.exc) frm.reload_doc();
                    }
                });
            }
        );
    },

    show_history: function(frm) {
        kubeport_show_release_history(frm);
    }
});

function kubeport_configure_release_actions(frm) {
    if (!frm.doc || frm.is_new()) return;

    const is_in_flight = ['In Progress', 'Uninstalling'].includes(frm.doc.status);
    const can_history = ['Deployed', 'Degraded', 'Failed'].includes(frm.doc.status);
    frm.toggle_enable('deploy_release', !is_in_flight);
    frm.toggle_enable('uninstall_release', can_history);
    frm.toggle_enable('show_history', can_history);

    let deploy_label = __('Install / Upgrade');
    if (frm.doc.status === 'Draft') {
        deploy_label = __('Install');
    } else if (frm.doc.status === 'Failed') {
        deploy_label = __('Retry');
    } else if (frm.doc.pending_changes) {
        deploy_label = __('Upgrade');
    } else if (['Deployed', 'Degraded'].includes(frm.doc.status)) {
        deploy_label = __('Redeploy');
    }
    frm.set_df_property('deploy_release', 'label', deploy_label);

    frm.clear_custom_buttons();
    if (can_history) {
        frm.add_custom_button(__('Force Uninstall'), () => {
            kubeport_force_uninstall(frm);
        }, __('Danger'));
    }

    if (frm.doc.pending_changes) {
        frm.set_intro(
            __('Saved desired state differs from the last successfully applied Helm spec.'),
            'orange'
        );
    }
}

function kubeport_force_uninstall(frm) {
    const expected = `UNINSTALL ${frm.doc.release_name}`;
    frappe.prompt(
        [{
            fieldname: 'confirmation',
            fieldtype: 'Data',
            label: __('Type {0}', [expected]),
            reqd: 1
        }],
        (values) => {
            frappe.call({
                doc: frm.doc,
                method: 'uninstall_release',
                args: {
                    force: 1,
                    confirmation: values.confirmation
                },
                callback: function(r) {
                    if (!r.exc) frm.reload_doc();
                }
            });
        },
        __('Force Uninstall'),
        __('Uninstall')
    );
}

function kubeport_show_release_history(frm) {
    frappe.call({
        doc: frm.doc,
        method: 'get_release_history',
        freeze: true,
        freeze_message: __('Loading release history...'),
        callback: function(r) {
            if (r.exc) return;
            const rows = r.message || [];
            const dialog = new frappe.ui.Dialog({
                title: __('Release History'),
                fields: [{
                    fieldname: 'history_html',
                    fieldtype: 'HTML',
                    options: kubeport_render_history_table(rows)
                }]
            });
            dialog.show();
            dialog.$wrapper.find('.kubeport-rollback').on('click', function() {
                const revision = parseInt($(this).attr('data-revision'), 10);
                if (!revision) return;
                frappe.confirm(
                    __('Roll back release "{0}" to revision {1}?',
                        [frm.doc.release_name, revision]),
                    () => {
                        dialog.hide();
                        frappe.call({
                            doc: frm.doc,
                            method: 'rollback_release',
                            args: { revision },
                            callback: function(resp) {
                                if (!resp.exc) frm.reload_doc();
                            }
                        });
                    }
                );
            });
        }
    });
}

function kubeport_render_history_table(rows) {
    if (!rows.length) {
        return `<div class="text-muted small">${__('No release history was returned by Helm.')}</div>`;
    }

    let html = '<div class="table-responsive">';
    html += '<table class="table table-bordered" style="margin-bottom: 0;">';
    html += '<thead><tr>';
    [__('Revision'), __('Status'), __('Chart'), __('App Version'), __('Updated'), ''].forEach((column) => {
        html += `<th>${frappe.utils.escape_html(column)}</th>`;
    });
    html += '</tr></thead><tbody>';
    rows.forEach((row) => {
        const revision = row.revision || row.version || '';
        html += '<tr>';
        html += `<td>${frappe.utils.escape_html(String(revision))}</td>`;
        html += `<td>${frappe.utils.escape_html(row.status || '')}</td>`;
        html += `<td>${frappe.utils.escape_html(row.chart || '')}</td>`;
        html += `<td>${frappe.utils.escape_html(row.app_version || row.appVersion || '')}</td>`;
        html += `<td>${frappe.utils.escape_html(row.updated || '')}</td>`;
        html += `<td><button class="btn btn-xs btn-default kubeport-rollback"
                    data-revision="${frappe.utils.escape_html(String(revision))}">
                    ${__('Rollback')}
                </button></td>`;
        html += '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
}

// Workload-readiness drilldown.  Loads via xcall AFTER document refresh so a
// slow cluster cannot block the form; the panel re-fetches whenever a
// realtime status event fires for this row.
function kubeport_render_release_health(frm) {
    if (!frm.doc.name || frm.is_new()) return;

    const $wrapper = kubeport_get_health_wrapper(frm);
    if (!$wrapper) return;

    const HEALTHY_STATUSES = ['Deployed', 'Degraded', 'Failed'];
    if (!HEALTHY_STATUSES.includes(frm.doc.status)) {
        $wrapper.html(
            `<div class="text-muted small">Workload readiness will appear once the release is deployed.</div>`
        );
        return;
    }

    $wrapper.html(`<div class="text-muted small">${__('Loading workload readiness…')}</div>`);

    const request_id = (frm.__helm_release_health_request_id || 0) + 1;
    frm.__helm_release_health_request_id = request_id;

    frappe.call({
        doc: frm.doc,
        method: 'get_release_health',
    }).then((r) => {
        if (request_id !== frm.__helm_release_health_request_id) return;
        if (r && !r.exc) {
            const payload = r.message || {};
            kubeport_paint_health_rows($wrapper, payload.rows || [], payload.error || '');
        } else {
            $wrapper.html(
                `<div class="text-muted small">${__('Could not fetch workload readiness.')}</div>`
            );
        }
    }, () => {
        if (request_id !== frm.__helm_release_health_request_id) return;
        $wrapper.html(
            `<div class="text-muted small">${__('Could not fetch workload readiness.')}</div>`
        );
    });
}

function kubeport_get_health_wrapper(frm) {
    const detail_field = frm.fields_dict.helm_status_detail;
    if (!detail_field || !detail_field.$wrapper) return null;

    let $panel = detail_field.$wrapper.find('.kubeport-release-health');
    if (!$panel.length) {
        $panel = $(
            `<div class="kubeport-release-health"
                  style="margin-top: 12px; padding: 8px;
                         border: 1px solid var(--border-color);
                         border-radius: 4px;">
                <div class="kubeport-release-health-header"
                     style="display: flex; justify-content: space-between;
                            align-items: center; margin-bottom: 8px;">
                    <strong>${__('Workload Readiness')}</strong>
                    <button class="btn btn-xs btn-default kubeport-release-health-refresh">
                        ${__('Refresh')}
                    </button>
                </div>
                <div class="kubeport-release-health-body"></div>
            </div>`
        );
        detail_field.$wrapper.append($panel);
        $panel.find('.kubeport-release-health-refresh').on('click', () => {
            kubeport_render_release_health(frm);
        });
    }
    return $panel.find('.kubeport-release-health-body');
}

function kubeport_paint_health_rows($body, rows, error) {
    const error_html = error
        ? `<div class="text-muted small" style="margin-bottom: 8px;">
                ${__('Readiness check error:')} ${frappe.utils.escape_html(error)}
           </div>`
        : '';

    if (!rows.length) {
        $body.html(
            `${error_html}
             <div class="text-muted small">${__('No workload resources found in this release.')}</div>`
        );
        return;
    }

    const lines = rows.map((row) => {
        const dot = row.ready
            ? '<span style="color: var(--green-500);">●</span>'
            : '<span style="color: var(--red-500);">●</span>';
        const reason = row.ready
            ? (row.message || __('ready'))
            : `${row.reason}${row.message ? ' - ' + row.message : ''}`;
        return `
            <div style="display: grid;
                        grid-template-columns: 16px 110px 1fr 1.4fr;
                        gap: 8px; padding: 4px 0;
                        border-bottom: 1px solid var(--border-color);
                        font-size: 12px;">
                <div>${dot}</div>
                <div><code>${frappe.utils.escape_html(row.kind)}</code></div>
                <div style="word-break: break-all;">
                    ${frappe.utils.escape_html(row.name)}
                </div>
                <div class="text-muted">${frappe.utils.escape_html(reason)}</div>
            </div>
        `;
    }).join('');
    $body.html(`${error_html}${lines}`);
}

// Namespace autocomplete — fetches live namespaces from the selected cluster
frappe.ui.form.on('Helm Release', {
    setup: function(frm) {
        frm.set_query('namespace', function() {
            return {};
        });

        frm.fields_dict.namespace.get_data = function(txt) {
            if (!frm.doc.cluster) return [];

            return new Promise((resolve) => {
                frappe.call({
                    method: 'kubeport.api.get_cluster_namespaces',
                    args: { cluster_name: frm.doc.cluster },
                    callback: function(r) {
                        if (r.message) {
                            const results = r.message
                                .filter(ns => !txt || ns.toLowerCase().includes(txt.toLowerCase()))
                                .map(ns => ({ label: ns, value: ns }));
                            resolve(results);
                        } else {
                            resolve([]);
                        }
                    }
                });
            });
        };

        // Chart version autocomplete — shows versions from the selected chart
        frm.fields_dict.chart_version.get_data = function(txt) {
            if (!frm.doc.chart) return [];

            return new Promise((resolve) => {
                frappe.db.get_doc('Helm Chart', frm.doc.chart).then(chart_doc => {
                    if (chart_doc.versions && chart_doc.versions.length) {
                        const results = chart_doc.versions
                            .filter(v => !txt || v.version.toLowerCase().includes(txt.toLowerCase()))
                            .map(v => ({
                                label: `${v.version} (App: ${v.app_version || 'N/A'})`,
                                value: v.version
                            }));
                        resolve(results);
                    } else {
                        resolve([]);
                    }
                });
            });
        };
    }
});
