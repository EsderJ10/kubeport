frappe.ui.form.on('Kubernetes Cluster', {
    refresh: function(frm) {
        const status_map = {
            'Connected': 'green',
            'Error': 'red',
            'Pending': 'orange'
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
        }

        // Contextual hints based on auth method
        if (frm.doc.auth_method === 'In-Cluster') {
            frm.set_intro(
                __('In-Cluster auth auto-detects credentials from the Kubernetes pod environment. No manual configuration needed.'),
                'blue'
            );
        } else if (frm.doc.status === 'Pending' && !frm.is_new()) {
            frm.set_intro(
                __('Discovery is live and read-only. Use "Test Connection" to persist this cluster status as Connected or Error.'),
                'blue'
            );
        } else if (frm.doc.auth_method === 'Kubeconfig' && !frm.doc.kubeconfig) {
            frm.set_intro(
                __('Click "Import Kubeconfig" to upload a kubeconfig file and pick a context, or paste the YAML manually below.'),
                'blue'
            );
        }

        _render_discovery_shell(frm);
        if (frm.is_new()) {
            _render_discovery_empty_state(frm, __('Save this cluster to load live discovery data.'));
            return;
        }

        _load_cluster_discovery(frm, { silent: true });
    },

    // -----------------------------------------------------------------------
    // Import Kubeconfig - browser-based file upload + context picker
    // -----------------------------------------------------------------------

    import_kubeconfig: function(frm) {
        // Step 1: Show file picker dialog
        const upload_dialog = new frappe.ui.Dialog({
            title: __('Import Kubeconfig'),
            fields: [
                {
                    fieldname: 'info',
                    fieldtype: 'HTML',
                    options: '<p class="text-muted">' +
                        __('Select a kubeconfig file from your machine (typically <code>~/.kube/config</code>). ' +
                           'The file is parsed in memory and never stored on the server.') +
                        '</p>'
                },
                {
                    fieldname: 'kubeconfig_file',
                    fieldtype: 'HTML',
                    // No accept filter - kubeconfig files often have no extension (e.g. "config")
                    options: '<input type="file" class="kubeconfig-file-input" ' +
                             'style="padding: 12px; border: 2px dashed var(--border-color); border-radius: 8px; ' +
                             'width: 100%; cursor: pointer; background: var(--bg-color);" />'
                }
            ],
            primary_action_label: __('Parse Contexts'),
            primary_action: function() {
                const file_input = upload_dialog.$wrapper.find('.kubeconfig-file-input')[0];
                if (!file_input || !file_input.files.length) {
                    frappe.msgprint(__('Please select a kubeconfig file.'));
                    return;
                }

                const reader = new FileReader();
                reader.onload = function(e) {
                    const content = e.target.result;
                    upload_dialog.hide();
                    _show_context_picker(frm, content);
                };
                reader.onerror = function() {
                    frappe.msgprint(__('Failed to read the file. Please try again.'));
                };
                reader.readAsText(file_input.files[0]);
            }
        });

        upload_dialog.show();
    },

    // -----------------------------------------------------------------------
    // Test Connection
    // -----------------------------------------------------------------------

    test_connection: function(frm) {
        if (frm.is_dirty()) {
            frappe.msgprint(__('Please save the document before testing the connection.'));
            return;
        }

        frappe.call({
            doc: frm.doc,
            method: 'test_connection',
            freeze: true,
            freeze_message: __('Connecting to the cluster...'),
            callback: function(r) {
                if (!r.exc) {
                    frm.reload_doc();
                }
            }
        });
    },

    refresh_discovery: function(frm) {
        if (frm.is_new()) {
            frappe.msgprint(__('Please save the document before refreshing discovery.'));
            return;
        }

        _load_cluster_discovery(frm, { silent: false });
    }
});


// ---------------------------------------------------------------------------
// Private helpers - Discovery
// ---------------------------------------------------------------------------

function _load_cluster_discovery(frm, { silent }) {
    frm.__discovery_request_id = (frm.__discovery_request_id || 0) + 1;
    const request_id = frm.__discovery_request_id;

    _set_discovery_status(frm, 'blue', __('Loading live cluster data...'));

    frappe.call({
        method: 'kubeport.api.discovery.get_cluster_discovery',
        args: { cluster_name: frm.doc.name },
        callback: function(r) {
            if (request_id !== frm.__discovery_request_id) {
                return;
            }

            if (r.exc || !r.message) {
                _set_discovery_status(
                    frm,
                    'red',
                    __('Discovery failed. Check Error Log for server details.')
                );
                _render_empty_table(frm, 'discovered_releases_html', __('No release data available.'));
                _render_empty_table(frm, 'discovered_sites_html', __('No site data available.'));
                return;
            }

            _render_discovery_response(frm, r.message);
            if (!silent) {
                frappe.show_alert({
                    message: __('Discovery refreshed.'),
                    indicator: 'green'
                });
            }
        }
    });
}

function _render_discovery_shell(frm) {
    _set_wrapper_html(
        frm,
        'discovery_status_html',
        '<div class="text-muted small">' + __('Discovery has not been loaded yet.') + '</div>'
    );
    _render_empty_table(frm, 'discovered_releases_html', __('No release data loaded.'));
    _render_empty_table(frm, 'discovered_sites_html', __('No site data loaded.'));
}

function _render_discovery_empty_state(frm, message) {
    _set_discovery_status(frm, 'orange', message);
    _render_empty_table(frm, 'discovered_releases_html', __('Save the cluster to view releases.'));
    _render_empty_table(frm, 'discovered_sites_html', __('Save the cluster to view sites.'));
}

function _render_discovery_response(frm, payload) {
    const benches = Array.isArray(payload.benches) ? payload.benches : [];
    const sites = Array.isArray(payload.sites) ? payload.sites : [];
    const errors = Array.isArray(payload.errors) ? payload.errors : [];
    const generated_at = payload.generated_at || '';

    let status_message = __('Live discovery fetched successfully.');
    let indicator = 'green';
    if (errors.length) {
        status_message = __('Discovery completed with partial errors.');
        indicator = 'orange';
    } else if (!benches.length) {
        status_message = __('No Helm releases were found in this cluster.');
        indicator = 'blue';
    }

    const meta_bits = [];
    if (generated_at) {
        meta_bits.push(__('Fetched at {0}', [frappe.datetime.str_to_user(generated_at)]));
    }
    meta_bits.push(__('Releases: {0}', [benches.length]));
    meta_bits.push(__('Sites: {0}', [sites.length]));

    let html = '<div class="small">';
    html += '<span class="indicator ' + indicator + '"></span> ';
    html += frappe.utils.escape_html(status_message);
    html += '</div>';
    html += '<div class="text-muted small" style="margin-top: 6px;">' +
        frappe.utils.escape_html(meta_bits.join(' | ')) +
        '</div>';

    if (errors.length) {
        html += '<div class="alert alert-warning small" style="margin-top: 12px; margin-bottom: 0;">';
        html += '<strong>' + __('Partial errors') + '</strong><ul style="margin: 8px 0 0 18px;">';
        errors.forEach((error) => {
            html += '<li>' + frappe.utils.escape_html(error.message || __('Unknown error')) + '</li>';
        });
        html += '</ul></div>';
    }

    _set_wrapper_html(frm, 'discovery_status_html', html);
    _render_release_table(frm, benches);
    _render_site_table(frm, sites);
}

function _render_release_table(frm, benches) {
    const columns = [
        __('Release'),
        __('Namespace'),
        __('Status'),
        __('Chart'),
        __('App Version'),
        __('Updated'),
        __('Kubeport')
    ];

    const rows = benches.map((bench) => {
        const docname = bench.helm_release_docname || '';
        const action = bench.is_tracked
            ? `<button class="btn btn-xs btn-default kubeport-open-release"
                    data-docname="${_escape_attr(docname)}">${__('Open')}</button>`
            : `<button class="btn btn-xs btn-primary kubeport-track-release"
                    data-release="${_escape_attr(bench.release_name)}"
                    data-namespace="${_escape_attr(bench.namespace || 'default')}">
                    ${__('Track')}
               </button>`;
        return [
            _escape_cell(bench.release_name),
            _escape_cell(bench.namespace),
            _escape_cell(bench.status),
            _escape_cell(bench.chart),
            _escape_cell(bench.app_version || 'N/A'),
            _escape_cell(bench.updated || 'N/A'),
            action
        ];
    });

    _render_table(
        frm,
        'discovered_releases_html',
        columns,
        rows,
        __('No Helm releases found in this cluster.')
    );
    _bind_release_tracking_actions(frm);
}

function _render_site_table(frm, sites) {
    const columns = [
        __('Site'),
        __('Bench Release'),
        __('Namespace'),
        __('Source'),
        __('Pod')
    ];

    const rows = sites.map((site) => ([
        _escape_cell(site.site_name),
        _escape_cell(site.bench_release),
        _escape_cell(site.namespace),
        _escape_cell(site.source),
        _escape_cell(site.pod_name || 'N/A')
    ]));

    _render_table(
        frm,
        'discovered_sites_html',
        columns,
        rows,
        __('No Frappe sites were discovered for the supported releases in this cluster.')
    );
}

function _render_table(frm, fieldname, columns, rows, empty_message) {
    if (!rows.length) {
        _render_empty_table(frm, fieldname, empty_message);
        return;
    }

    let html = '<div class="table-responsive" style="border: 1px solid var(--border-color); border-radius: 8px;">';
    html += '<table class="table table-bordered" style="margin-bottom: 0;">';
    html += '<thead><tr>';
    columns.forEach((column) => {
        html += '<th>' + frappe.utils.escape_html(column) + '</th>';
    });
    html += '</tr></thead><tbody>';

    rows.forEach((row) => {
        html += '<tr>';
        row.forEach((cell) => {
            html += '<td>' + cell + '</td>';
        });
        html += '</tr>';
    });

    html += '</tbody></table></div>';
    _set_wrapper_html(frm, fieldname, html);
}

function _render_empty_table(frm, fieldname, message) {
    _set_wrapper_html(
        frm,
        fieldname,
        '<div class="text-muted small" style="padding: 12px; border: 1px dashed var(--border-color); border-radius: 8px;">' +
            frappe.utils.escape_html(message) +
        '</div>'
    );
}

function _set_discovery_status(frm, indicator, message) {
    const html =
        '<div class="small">' +
            '<span class="indicator ' + indicator + '"></span> ' +
            frappe.utils.escape_html(message) +
        '</div>';
    _set_wrapper_html(frm, 'discovery_status_html', html);
}

function _set_wrapper_html(frm, fieldname, html) {
    const field = frm.fields_dict[fieldname];
    if (!field || !field.$wrapper) {
        return;
    }
    field.$wrapper.html(html);
}

function _escape_cell(value) {
    return frappe.utils.escape_html(value == null ? '' : String(value));
}

function _escape_attr(value) {
    return _escape_cell(value).replace(/"/g, '&quot;');
}

function _bind_release_tracking_actions(frm) {
    const field = frm.fields_dict.discovered_releases_html;
    if (!field || !field.$wrapper) return;

    field.$wrapper.find('.kubeport-open-release').on('click', function() {
        const docname = $(this).attr('data-docname');
        if (docname) {
            frappe.set_route('Form', 'Helm Release', docname);
        }
    });

    field.$wrapper.find('.kubeport-track-release').on('click', function() {
        const release_name = $(this).attr('data-release');
        const namespace = $(this).attr('data-namespace') || 'default';
        frappe.call({
            method: 'kubeport.api.discovery.adopt_helm_release',
            args: {
                cluster_name: frm.doc.name,
                namespace,
                release_name
            },
            freeze: true,
            freeze_message: __('Tracking Helm release...'),
            callback: function(r) {
                if (r.exc || !r.message) return;
                frappe.set_route('Form', 'Helm Release', r.message.name);
            }
        });
    });
}


// ---------------------------------------------------------------------------
// Private helpers - context picker dialog
// ---------------------------------------------------------------------------

function _show_context_picker(frm, kubeconfig_content) {
    frappe.call({
        method: 'kubeport.api.parse_kubeconfig_contexts',
        args: { kubeconfig_content: kubeconfig_content },
        freeze: true,
        freeze_message: __('Parsing kubeconfig...'),
        callback: function(r) {
            if (!r.message || !r.message.length) {
                frappe.msgprint(__('No contexts found in the kubeconfig file.'));
                return;
            }

            const contexts = r.message;
            let selected_context = null;

            // Build HTML table of contexts
            let table_html = `
                <p class="text-muted mb-3">${__('Select a context to import. Only the selected context and its credentials will be stored.')}</p>
                <table class="table table-hover" style="cursor: pointer;">
                    <thead>
                        <tr>
                            <th>${__('Context')}</th>
                            <th>${__('Cluster')}</th>
                            <th>${__('Server')}</th>
                            <th style="width: 50px; text-align: center;">${__('Active')}</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            contexts.forEach(function(ctx, idx) {
                const is_current = ctx.is_current ? '*' : '';
                const row_class = ctx.is_current ? 'font-weight-bold' : '';
                const normalized_note = ctx.server_was_normalized
                    ? `<div class="text-muted small">${__('Imported from {0}', [frappe.utils.escape_html(ctx.original_server)])}</div>`
                    : '';
                table_html += `
                    <tr data-idx="${idx}" class="context-row ${row_class}"
                        style="transition: background 0.15s;">
                        <td><code>${frappe.utils.escape_html(ctx.context_name)}</code></td>
                        <td>${frappe.utils.escape_html(ctx.cluster_name)}</td>
                        <td><code>${frappe.utils.escape_html(ctx.server)}</code>${normalized_note}</td>
                        <td style="text-align: center; font-size: 1.2em;">${is_current}</td>
                    </tr>
                `;
            });

            table_html += '</tbody></table>';

            const context_dialog = new frappe.ui.Dialog({
                title: __('Select Context'),
                fields: [
                    {
                        fieldname: 'context_table',
                        fieldtype: 'HTML',
                        options: table_html
                    },
                    {
                        fieldname: 'display_name',
                        fieldtype: 'Data',
                        label: __('Cluster Display Name'),
                        description: __('Give this cluster a friendly name (e.g. "Production East", "Dev K3d"). The technical context name is stored separately.'),
                        hidden: 1
                    }
                ],
                primary_action_label: __('Import'),
                primary_action: function() {
                    if (selected_context === null) {
                        frappe.msgprint(__('Please select a context from the table.'));
                        return;
                    }

                    const ctx = contexts[selected_context];
                    const display_name = context_dialog.get_value('display_name') || ctx.cluster_name;

                    frappe.call({
                        method: 'kubeport.api.extract_kubeconfig_context',
                        args: {
                            kubeconfig_content: kubeconfig_content,
                            context_name: ctx.context_name
                        },
                        freeze: true,
                        freeze_message: __('Extracting context...'),
                        callback: function(r) {
                            if (!r.message || !r.message.kubeconfig) return;

                            const payload = r.message;

                            // Hydrate the DocType
                            frm.set_value('kubeconfig', payload.kubeconfig);
                            frm.set_value('kubeconfig_context', ctx.context_name);
                            frm.set_value('cluster_name', display_name);

                            frm.dirty();
                            context_dialog.hide();

                            let alert_message = __('Context "{0}" imported as "{1}". Save the document to persist.', [ctx.context_name, display_name]);
                            if (payload.server_was_normalized) {
                                alert_message += ' ' + __('Server rewritten from "{0}" to "{1}" for container reachability.', [
                                    payload.original_server,
                                    payload.server
                                ]);
                            }

                            frappe.show_alert({
                                message: alert_message,
                                indicator: 'green'
                            }, 5);
                        }
                    });
                }
            });

            context_dialog.show();

            // Row selection - clicking a row selects it and reveals the display name field
            context_dialog.$wrapper.on('click', '.context-row', function() {
                const idx = parseInt($(this).data('idx'));
                selected_context = idx;

                const ctx = contexts[idx];

                // Highlight selected row
                context_dialog.$wrapper.find('.context-row').css('background', '');
                $(this).css('background', 'var(--highlight-color, #e8f4fd)');

                // Reveal and pre-fill the display name field
                context_dialog.set_df_property('display_name', 'hidden', 0);
                context_dialog.set_value('display_name', ctx.cluster_name);
            });

            // Pre-select the current context if one exists
            const current_idx = contexts.findIndex(ctx => ctx.is_current);
            if (current_idx >= 0) {
                selected_context = current_idx;

                context_dialog.$wrapper
                    .find(`.context-row[data-idx="${current_idx}"]`)
                    .css('background', 'var(--highlight-color, #e8f4fd)');

                // Reveal display name for the pre-selected context
                context_dialog.set_df_property('display_name', 'hidden', 0);
                context_dialog.set_value('display_name', contexts[current_idx].cluster_name);
            }
        }
    });
}
