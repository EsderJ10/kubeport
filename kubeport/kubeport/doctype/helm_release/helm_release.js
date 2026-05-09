frappe.ui.form.on('Helm Release', {
    setup: function(frm) {
        frm.set_query('site_image', () => {
            return {
                filters: {
                    status: 'Active'
                }
            };
        });
    },

    refresh: function(frm) {
        kubeport_close_observability_panel(frm);
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
        kubeport_configure_site_image_selector(frm);

        if (!frm.__helm_release_status_listener_bound) {
            frm.__helm_release_status_listener_bound = true;
            frappe.realtime.on('helm_release_status_update', (data) => {
                if (data.release_docname === frm.doc.name) {
                    if (frm.__kubeport_observability_state) {
                        kubeport_render_release_health(frm);
                        kubeport_refresh_active_observability_panel(frm);
                    } else {
                        frm.reload_doc();
                    }
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
                kubeport_configure_site_image_selector(frm, chart_doc);
            });
        } else {
            frm.set_value('site_image', '');
            kubeport_configure_site_image_selector(frm);
        }
    },

    site_image: function(frm) {
        kubeport_render_site_image_detail(frm);
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

function kubeport_configure_site_image_selector(frm, chart_doc) {
    const apply_state = (chart) => {
        const is_site_chart = kubeport_is_site_chart(chart);
        frm.toggle_display('site_image', is_site_chart);
        frm.toggle_display('site_image_detail', is_site_chart && Boolean(frm.doc.site_image));
        if (!is_site_chart && frm.doc.site_image) {
            frm.set_value('site_image', '');
        }
        kubeport_render_site_image_detail(frm);
    };

    if (chart_doc) {
        apply_state(chart_doc);
        return;
    }

    if (!frm.doc.chart) {
        apply_state(null);
        return;
    }

    frappe.db.get_doc('Helm Chart', frm.doc.chart).then(apply_state, () => apply_state(null));
}

function kubeport_is_site_chart(chart_doc) {
    if (!chart_doc) return false;
    const name = String(chart_doc.chart_name || chart_doc.name || '').toLowerCase();
    return name.includes('erpnext') || name.includes('frappe');
}

function kubeport_render_site_image_detail(frm) {
    const field = frm.fields_dict.site_image_detail;
    if (!field || !field.$wrapper) return;

    if (!frm.doc.site_image) {
        field.$wrapper.html('');
        return;
    }

    field.$wrapper.html(`<div class="text-muted small">${__('Loading image catalog details...')}</div>`);
    frappe.xcall('kubeport.api.site_images.list_site_images', {
        include_deprecated: 1
    }).then((rows) => {
        const row = (rows || []).find((entry) => entry.name === frm.doc.site_image);
        if (!row) {
            field.$wrapper.html(
                `<div class="text-muted small">${__('Selected image was not found in the catalog.')}</div>`
            );
            return;
        }
        field.$wrapper.html(kubeport_site_image_detail_html(row));
    }, () => {
        field.$wrapper.html(
            `<div class="text-muted small">${__('Could not load image catalog details.')}</div>`
        );
    });
}

function kubeport_site_image_detail_html(row) {
    const apps = (row.apps || []).map((app) => {
        const label = `${app.app_name || ''}${app.ref ? ':' + app.ref : ''}`;
        return `<code>${frappe.utils.escape_html(label)}</code>`;
    }).join(' ');
    const digest = row.image_digest || __('not recorded');
    const status_color = row.status === 'Deprecated' ? 'orange' : 'green';
    const origin = row.origin === 'Kubeport' ? 'Curated' : 'Custom';
    const origin_color = row.origin === 'Kubeport' ? 'blue' : 'gray';
    return `
        <div style="margin-top: 8px; padding: 8px;
                    border: 1px solid var(--border-color); border-radius: 4px;">
            <div style="display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap;">
                <strong>${frappe.utils.escape_html(row.image_title || row.name)}</strong>
                <div style="display: flex; gap: 6px;">
                    <span class="indicator-pill ${origin_color}">
                        ${__(origin)}
                    </span>
                    <span class="indicator-pill ${status_color}">
                        ${frappe.utils.escape_html(row.status || '')}
                    </span>
                </div>
            </div>
            <div class="small" style="margin-top: 6px; word-break: break-all;">
                <code>${frappe.utils.escape_html(row.image_repository || '')}:${frappe.utils.escape_html(row.image_tag || '')}</code>
            </div>
            <div class="text-muted small" style="margin-top: 4px; word-break: break-all;">
                ${__('Digest')}: ${frappe.utils.escape_html(digest)}
            </div>
            <div class="text-muted small" style="margin-top: 4px;">
                ${__('Frappe')}: ${frappe.utils.escape_html(String(row.frappe_major || ''))}
                ${row.erpnext_version ? ` &middot; ${__('ERPNext')}: ${frappe.utils.escape_html(row.erpnext_version)}` : ''}
            </div>
            ${apps ? `<div class="small" style="margin-top: 6px;">${apps}</div>` : ''}
        </div>
    `;
}

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
            const payload = r.message || {};
            const rows = payload.rows || [];
            const error = payload.error || '';
            const dialog = new frappe.ui.Dialog({
                title: __('Release History'),
                fields: [{
                    fieldname: 'history_html',
                    fieldtype: 'HTML',
                    options: kubeport_render_history_table(rows, error)
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

function kubeport_render_history_table(rows, error) {
    if (error) {
        return `<div class="text-muted small">${__('Could not load release history:')}
            ${frappe.utils.escape_html(error)}
        </div>`;
    }

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
            kubeport_paint_health_rows(frm, $wrapper, payload.rows || [], payload.error || '');
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

function kubeport_paint_health_rows(frm, $body, rows, error) {
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
        const actions = row.ready ? '' : kubeport_render_health_actions(row);
        return `
            <div style="display: grid;
                        grid-template-columns: 16px 110px 1fr 1.4fr 190px;
                        gap: 8px; padding: 4px 0;
                        border-bottom: 1px solid var(--border-color);
                        font-size: 12px;">
                <div>${dot}</div>
                <div><code>${frappe.utils.escape_html(row.kind)}</code></div>
                <div style="word-break: break-all;">
                    ${frappe.utils.escape_html(row.name)}
                </div>
                <div class="text-muted">${frappe.utils.escape_html(reason)}</div>
                <div>${actions}</div>
            </div>
        `;
    }).join('');
    $body.html(`${error_html}${lines}`);
    $body.find('.kubeport-health-action').on('click', function() {
        const $button = $(this);
        const row = {
            kind: $button.attr('data-kind') || '',
            name: $button.attr('data-name') || '',
            namespace: $button.attr('data-namespace') || '',
            pod_count: cint($button.attr('data-pod-count') || 0),
        };
        const action = $button.attr('data-action');
        if (action === 'logs') {
            kubeport_show_resource_logs(frm, row);
        } else if (action === 'events') {
            kubeport_show_resource_events(frm, row);
        } else if (action === 'rollout') {
            kubeport_show_resource_rollout(frm, row);
        }
    });
}

function kubeport_render_health_actions(row) {
    const kind = frappe.utils.escape_html(row.kind || '');
    const name = frappe.utils.escape_html(row.name || '');
    const namespace = frappe.utils.escape_html(row.namespace || '');
    const pod_count = cint(row.pod_count || 0);
    const log_kinds = ['Deployment', 'StatefulSet', 'DaemonSet', 'Pod'];
    const rollout_kinds = ['Deployment', 'StatefulSet', 'DaemonSet'];
    let html = '<div style="display: flex; gap: 4px; flex-wrap: wrap;">';
    if (log_kinds.includes(row.kind)) {
        html += `
            <button class="btn btn-xs btn-default kubeport-health-action"
                    data-action="logs" data-kind="${kind}" data-name="${name}"
                    data-namespace="${namespace}" data-pod-count="${pod_count}">
                ${__('Logs')}
            </button>`;
    }
    html += `
        <button class="btn btn-xs btn-default kubeport-health-action"
                data-action="events" data-kind="${kind}" data-name="${name}"
                data-namespace="${namespace}" data-pod-count="${pod_count}">
            ${__('Events')}
        </button>`;
    if (rollout_kinds.includes(row.kind)) {
        html += `
            <button class="btn btn-xs btn-default kubeport-health-action"
                    data-action="rollout" data-kind="${kind}" data-name="${name}"
                    data-namespace="${namespace}" data-pod-count="${pod_count}">
                ${__('Rollout')}
            </button>`;
    }
    html += '</div>';
    return html;
}

function kubeport_show_resource_logs(frm, row) {
    kubeport_open_observability_panel(frm, 'logs', row);
}

function kubeport_show_resource_events(frm, row) {
    kubeport_open_observability_panel(frm, 'events', row);
}

function kubeport_show_resource_rollout(frm, row) {
    kubeport_open_observability_panel(frm, 'rollout', row);
}

function kubeport_open_observability_panel(frm, type, row) {
    frm.__kubeport_observability_panel_id = cint(frm.__kubeport_observability_panel_id || 0) + 1;
    frm.__kubeport_observability_state = {
        panel_id: frm.__kubeport_observability_panel_id,
        type,
        row,
        values: {
            tail_lines: '200',
            pod_name: '',
            container: '',
            previous: 0,
            sort_key: 'last_seen',
            sort_dir: 'desc'
        },
        rows: [],
        error: '',
        log_payload: null,
        request_id: 0
    };
    kubeport_render_observability_shell(frm);
    kubeport_refresh_active_observability_panel(frm);
}

function kubeport_refresh_active_observability_panel(frm) {
    const state = frm.__kubeport_observability_state;
    if (!state) return;

    if (state.type === 'logs') {
        kubeport_read_log_values(frm);
        kubeport_render_logs_panel(frm);
    } else if (state.type === 'events') {
        kubeport_fetch_resource_events(frm);
    } else if (state.type === 'rollout') {
        kubeport_fetch_resource_rollout(frm);
    }
}

function kubeport_render_observability_shell(frm) {
    const state = frm.__kubeport_observability_state;
    if (!state) return;

    const $panel = kubeport_get_observability_panel(frm);
    if (!$panel) return;

    const title_map = {
        logs: __('Logs'),
        events: __('Events'),
        rollout: __('Rollout')
    };
    const title = `${title_map[state.type] || ''}: ${state.row.kind}/${state.row.name}`;
    $panel.html(`
        <div class="kubeport-observability-header"
             style="display: flex; justify-content: space-between; gap: 8px;
                    align-items: center; margin-bottom: 8px;">
            <strong>${frappe.utils.escape_html(title)}</strong>
            <button class="btn btn-xs btn-default kubeport-observability-close">
                ${__('Close')}
            </button>
        </div>
        <div class="kubeport-observability-body"></div>
    `);
    $panel.find('.kubeport-observability-close').on('click', () => {
        kubeport_close_observability_panel(frm);
    });
}

function kubeport_get_observability_panel(frm) {
    const detail_field = frm.fields_dict.helm_status_detail;
    if (!detail_field || !detail_field.$wrapper) return null;

    let $panel = detail_field.$wrapper.find('.kubeport-observability-panel');
    if (!$panel.length) {
        $panel = $(`
            <div class="kubeport-observability-panel"
                 style="margin-top: 12px; padding: 8px;
                        border: 1px solid var(--border-color);
                        border-radius: 4px;">
            </div>
        `);
        detail_field.$wrapper.append($panel);
    }
    return $panel;
}

function kubeport_close_observability_panel(frm) {
    frm.__kubeport_observability_state = null;
    const detail_field = frm.fields_dict && frm.fields_dict.helm_status_detail;
    if (detail_field && detail_field.$wrapper) {
        detail_field.$wrapper.find('.kubeport-observability-panel').remove();
    }
}

function kubeport_create_observability_request_token(frm) {
    const state = frm.__kubeport_observability_state;
    if (!state) return null;

    state.request_id = cint(state.request_id || 0) + 1;
    return {
        panel_id: state.panel_id,
        request_id: state.request_id,
        type: state.type || '',
        kind: (state.row && state.row.kind) || '',
        name: (state.row && state.row.name) || '',
        namespace: (state.row && state.row.namespace) || ''
    };
}

function kubeport_observability_request_is_active(frm, token) {
    const state = frm.__kubeport_observability_state;
    if (!state || !token) return false;

    const row = state.row || {};
    return cint(state.panel_id || 0) === cint(token.panel_id || 0)
        && cint(state.request_id || 0) === cint(token.request_id || 0)
        && (state.type || '') === token.type
        && (row.kind || '') === token.kind
        && (row.name || '') === token.name
        && (row.namespace || '') === token.namespace;
}

function kubeport_render_logs_panel(frm) {
    const state = frm.__kubeport_observability_state;
    const $body = kubeport_get_observability_body(frm);
    if (!state || !$body) return;

    const values = state.values;
    $body.html(`
        <div class="kubeport-log-controls"
             style="display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end;
                    margin-bottom: 8px;">
            <label class="control-label" style="margin-bottom: 0;">
                ${__('Tail Lines')}
                <select class="form-control input-xs kubeport-log-tail" style="width: 92px;">
                    ${['50', '200', '500', '2000'].map((option) => `
                        <option value="${option}" ${values.tail_lines === option ? 'selected' : ''}>${option}</option>
                    `).join('')}
                </select>
            </label>
            <label class="control-label kubeport-log-pod-control" style="margin-bottom: 0; display: none;">
                ${__('Pod')}
                <select class="form-control input-xs kubeport-log-pod" style="min-width: 220px;"></select>
            </label>
            <label class="control-label" style="margin-bottom: 0;">
                ${__('Container')}
                <input class="form-control input-xs kubeport-log-container"
                       style="width: 180px;" value="${frappe.utils.escape_html(values.container || '')}">
            </label>
            <label class="checkbox" style="margin: 0;">
                <input type="checkbox" class="kubeport-log-previous" ${values.previous ? 'checked' : ''}>
                ${__('Previous container')}
            </label>
            <button class="btn btn-xs btn-default kubeport-log-refresh">${__('Refresh')}</button>
        </div>
        <div class="kubeport-log-result"></div>
    `);
    $body.find('.kubeport-log-refresh').on('click', () => kubeport_fetch_resource_logs(frm));
    $body.find('.kubeport-log-tail, .kubeport-log-previous').on('change', () => {
        kubeport_fetch_resource_logs(frm);
    });
    $body.find('.kubeport-log-pod').on('change', () => {
        kubeport_read_log_values(frm);
        kubeport_fetch_resource_logs(frm);
    });
    $body.find('.kubeport-log-container').on('keydown', (event) => {
        if (event.key === 'Enter') kubeport_fetch_resource_logs(frm);
    });
    kubeport_fetch_resource_logs(frm);
}

function kubeport_fetch_resource_logs(frm) {
    const state = frm.__kubeport_observability_state;
    const $body = kubeport_get_observability_body(frm);
    if (!state || !$body) return;

    kubeport_read_log_values(frm);
    const values = state.values;
    const $target = $body.find('.kubeport-log-result');
    const token = kubeport_create_observability_request_token(frm);
    $target.html(`<div class="text-muted small">${__('Loading logs…')}</div>`);
    frappe.call({
        method: 'kubeport.api.observability.get_release_resource_logs',
        args: {
            release_docname: frm.doc.name,
            kind: state.row.kind,
            name: state.row.name,
            namespace: state.row.namespace || null,
            pod_name: values.pod_name || null,
            container: values.container || null,
            tail_lines: cint(values.tail_lines || 200),
            previous: cint(values.previous || 0) ? 1 : 0
        }
    }).then((r) => {
        if (!kubeport_observability_request_is_active(frm, token)) return;
        if (r.exc) {
            $target.html(`<div class="text-muted small">${__('Could not fetch logs.')}</div>`);
            return;
        }
        const payload = r.message || {};
        state.log_payload = payload;
        kubeport_sync_log_pod_select(frm, payload);
        $target.html(kubeport_render_logs_payload(payload));
    }, () => {
        if (!kubeport_observability_request_is_active(frm, token)) return;
        $target.html(`<div class="text-muted small">${__('Could not fetch logs.')}</div>`);
    });
}

function kubeport_read_log_values(frm) {
    const state = frm.__kubeport_observability_state;
    const $body = kubeport_get_observability_body(frm);
    if (!state || !$body) return;

    state.values.tail_lines = String($body.find('.kubeport-log-tail').val() || state.values.tail_lines || '200');
    state.values.pod_name = String($body.find('.kubeport-log-pod').val() || state.values.pod_name || '');
    state.values.container = String($body.find('.kubeport-log-container').val() || '');
    state.values.previous = $body.find('.kubeport-log-previous').is(':checked') ? 1 : 0;
}

function kubeport_sync_log_pod_select(frm, payload) {
    const state = frm.__kubeport_observability_state;
    const $body = kubeport_get_observability_body(frm);
    if (!state || !$body) return;

    const pods = payload.pods || [];
    const options = pods
        .map(pod => pod.name || '')
        .filter(Boolean);
    const $select = $body.find('.kubeport-log-pod');
    $select.html(options.map((option) => {
        const selected = option === payload.selected_pod ? 'selected' : '';
        return `<option value="${frappe.utils.escape_html(option)}" ${selected}>
            ${frappe.utils.escape_html(option)}
        </option>`;
    }).join(''));
    $body.find('.kubeport-log-pod-control').toggle(options.length > 1);

    const selected = payload.selected_pod || '';
    if (selected && options.includes(selected)) {
        state.values.pod_name = selected;
    }
}

function kubeport_render_logs_payload(payload) {
    const pods = payload.pods || [];
    const logs_by_pod = payload.logs_by_pod || {};
    const errors_by_pod = payload.errors_by_pod || {};
    const selected_pod = payload.selected_pod || (pods[0] && pods[0].name) || '';
    let html = kubeport_render_panel_error(payload.error || '');
    if (!pods.length) {
        return html || `<div class="text-muted small">${__('No pods were found for this resource.')}</div>`;
    }

    const pod = pods.find(row => row.name === selected_pod) || pods[0];
    const pod_name = pod.name || '';
    const containers = (pod.container_names || []).join(', ');
    const status = `${pod.phase || __('Unknown')} · ${__('restarts')}: ${pod.restart_count || 0}`;
    html += `
        <div style="margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; gap: 8px; margin-bottom: 4px;">
                <strong>${frappe.utils.escape_html(pod_name)}</strong>
                <span class="text-muted small">${frappe.utils.escape_html(status)}</span>
            </div>
            ${containers ? `<div class="text-muted small" style="margin-bottom: 4px;">
                ${__('Containers')}: ${frappe.utils.escape_html(containers)}
            </div>` : ''}
            ${errors_by_pod[pod_name] ? `<div class="text-muted small" style="margin-bottom: 4px;">
                ${frappe.utils.escape_html(errors_by_pod[pod_name])}
            </div>` : ''}
            <pre style="max-height: 420px; overflow: auto; white-space: pre-wrap;
                        background: var(--fg-color); color: var(--text-color);
                        border: 1px solid var(--border-color); border-radius: 4px;
                        padding: 8px; font-size: 12px;">${frappe.utils.escape_html(logs_by_pod[pod_name] || '')}</pre>
        </div>`;
    return html;
}

function kubeport_fetch_resource_events(frm) {
    const state = frm.__kubeport_observability_state;
    const $body = kubeport_get_observability_body(frm);
    if (!state || !$body) return;

    $body.html(`
        <div style="display: flex; justify-content: flex-end; margin-bottom: 8px;">
            <button class="btn btn-xs btn-default kubeport-events-refresh">${__('Refresh')}</button>
        </div>
        <div class="kubeport-events-result"></div>
    `);
    $body.find('.kubeport-events-refresh').on('click', () => kubeport_fetch_resource_events(frm));
    const $target = $body.find('.kubeport-events-result');
    const token = kubeport_create_observability_request_token(frm);
    $target.html(`<div class="text-muted small">${__('Loading events…')}</div>`);
    frappe.call({
        method: 'kubeport.api.observability.get_release_resource_events',
        args: {
            release_docname: frm.doc.name,
            kind: state.row.kind,
            name: state.row.name,
            namespace: state.row.namespace || null,
            limit: 20
        }
    }).then((r) => {
        if (!kubeport_observability_request_is_active(frm, token)) return;
        if (r.exc) {
            $target.html(kubeport_render_panel_error(__('Could not fetch events.')));
            return;
        }
        const payload = kubeport_normalize_observability_rows(r.message);
        state.rows = payload.rows;
        state.error = payload.error;
        kubeport_render_events_result(frm);
    }, () => {
        if (!kubeport_observability_request_is_active(frm, token)) return;
        $target.html(kubeport_render_panel_error(__('Could not fetch events.')));
    });
}

function kubeport_render_events_result(frm) {
    const state = frm.__kubeport_observability_state;
    const $body = kubeport_get_observability_body(frm);
    if (!state || !$body) return;

    const $target = $body.find('.kubeport-events-result');
    $target.html(kubeport_render_events(state.rows || [], state.values, state.error || ''));
    $target.find('.kubeport-events-sort').on('click', function() {
        const key = $(this).attr('data-sort-key') || 'last_seen';
        if (state.values.sort_key === key) {
            state.values.sort_dir = state.values.sort_dir === 'asc' ? 'desc' : 'asc';
        } else {
            state.values.sort_key = key;
            state.values.sort_dir = 'desc';
        }
        kubeport_render_events_result(frm);
    });
}

function kubeport_render_events(rows, values, error) {
    if (!rows.length) {
        return kubeport_render_panel_error(error)
            || `<div class="text-muted small">${__('No scoped events were returned for this resource.')}</div>`;
    }
    rows = kubeport_sort_rows(rows, values.sort_key || 'last_seen', values.sort_dir || 'desc');
    let html = kubeport_render_panel_error(error);
    html += '<div class="table-responsive"><table class="table table-bordered" style="margin-bottom: 0;">';
    html += `<thead><tr>
        <th><button class="btn btn-xs btn-link kubeport-events-sort" data-sort-key="type">${__('Type')}</button></th>
        <th><button class="btn btn-xs btn-link kubeport-events-sort" data-sort-key="reason">${__('Reason')}</button></th>
        <th><button class="btn btn-xs btn-link kubeport-events-sort" data-sort-key="count">${__('Count')}</button></th>
        <th><button class="btn btn-xs btn-link kubeport-events-sort" data-sort-key="last_seen">${__('Age')}</button></th>
        <th>${__('Message')}</th>
    </tr></thead><tbody>`;
    rows.forEach((row) => {
        const message = row.message || '';
        const timestamp = row.last_seen || row.first_seen || '';
        html += `<tr>
            <td>${frappe.utils.escape_html(row.type || '')}</td>
            <td>${frappe.utils.escape_html(row.reason || '')}</td>
            <td>${frappe.utils.escape_html(String(row.count || 0))}</td>
            <td title="${frappe.utils.escape_html(timestamp)}">
                ${frappe.utils.escape_html(kubeport_format_age(timestamp))}
            </td>
            <td style="word-break: break-word;" title="${frappe.utils.escape_html(message)}">
                ${frappe.utils.escape_html(message)}
            </td>
        </tr>`;
    });
    html += '</tbody></table></div>';
    return html;
}

function kubeport_fetch_resource_rollout(frm) {
    const state = frm.__kubeport_observability_state;
    const $body = kubeport_get_observability_body(frm);
    if (!state || !$body) return;

    $body.html(`
        <div style="display: flex; justify-content: flex-end; margin-bottom: 8px;">
            <button class="btn btn-xs btn-default kubeport-rollout-refresh">${__('Refresh')}</button>
        </div>
        <div class="kubeport-rollout-result"></div>
    `);
    $body.find('.kubeport-rollout-refresh').on('click', () => kubeport_fetch_resource_rollout(frm));
    const $target = $body.find('.kubeport-rollout-result');
    const token = kubeport_create_observability_request_token(frm);
    $target.html(`<div class="text-muted small">${__('Loading rollout context…')}</div>`);
    frappe.call({
        method: 'kubeport.api.observability.get_release_resource_rollout',
        args: {
            release_docname: frm.doc.name,
            kind: state.row.kind,
            name: state.row.name,
            namespace: state.row.namespace || null,
            limit: 10
        }
    }).then((r) => {
        if (!kubeport_observability_request_is_active(frm, token)) return;
        if (r.exc) {
            $target.html(kubeport_render_panel_error(__('Could not fetch rollout context.')));
            return;
        }
        const payload = kubeport_normalize_observability_rows(r.message);
        $target.html(kubeport_render_rollout(payload.rows, payload.error));
    }, () => {
        if (!kubeport_observability_request_is_active(frm, token)) return;
        $target.html(kubeport_render_panel_error(__('Could not fetch rollout context.')));
    });
}

function kubeport_render_rollout(rows, error) {
    if (!rows.length) {
        return kubeport_render_panel_error(error)
            || `<div class="text-muted small">${__('No rollout context was returned for this resource.')}</div>`;
    }
    let html = kubeport_render_panel_error(error);
    html += '<div class="table-responsive"><table class="table table-bordered" style="margin-bottom: 0;">';
    html += `<thead><tr>
        <th>${__('Revision')}</th><th>${__('State')}</th><th>${__('Created')}</th>
        <th>${__('Change Cause')}</th><th>${__('Images')}</th>
    </tr></thead><tbody>`;
    rows.forEach((row) => {
        const revision_state = row.current ? __('Current') : (row.update ? __('Update') : '');
        html += `<tr>
            <td>${frappe.utils.escape_html(String(row.revision || ''))}</td>
            <td>${frappe.utils.escape_html(revision_state)}</td>
            <td>${frappe.utils.escape_html(row.created_at || '')}</td>
            <td style="word-break: break-word;">${frappe.utils.escape_html(row.change_cause || '')}</td>
            <td style="word-break: break-all;">${frappe.utils.escape_html(row.image_summary || '')}</td>
        </tr>`;
    });
    html += '</tbody></table></div>';
    return html;
}

function kubeport_get_observability_body(frm) {
    const detail_field = frm.fields_dict.helm_status_detail;
    if (!detail_field || !detail_field.$wrapper) return null;
    const $body = detail_field.$wrapper.find('.kubeport-observability-body');
    return $body.length ? $body : null;
}

function kubeport_normalize_observability_rows(message) {
    if (Array.isArray(message)) {
        return { rows: message, error: '' };
    }
    message = message || {};
    return {
        rows: message.rows || [],
        error: message.error || ''
    };
}

function kubeport_render_panel_error(error) {
    if (!error) return '';
    return `<div class="text-danger small" style="margin-bottom: 8px;">
        ${frappe.utils.escape_html(error)}
    </div>`;
}

function kubeport_sort_rows(rows, key, direction) {
    const sorted = [...rows];
    sorted.sort((a, b) => {
        const left = a[key] || '';
        const right = b[key] || '';
        if (key === 'count') {
            return cint(left) - cint(right);
        }
        return String(left).localeCompare(String(right));
    });
    if (direction === 'desc') sorted.reverse();
    return sorted;
}

function kubeport_format_age(timestamp) {
    if (!timestamp) return '';
    if (typeof moment === 'function') {
        const parsed = moment(timestamp);
        if (parsed.isValid()) return parsed.fromNow();
    }
    return timestamp;
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
