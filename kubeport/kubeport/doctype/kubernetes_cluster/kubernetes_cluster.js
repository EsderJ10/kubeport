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
        } else if (frm.doc.auth_method === 'Kubeconfig' && !frm.doc.kubeconfig) {
            frm.set_intro(
                __('Click "Import Kubeconfig" to upload a kubeconfig file and pick a context, or paste the YAML manually below.'),
                'blue'
            );
        }
    },

    // -----------------------------------------------------------------------
    // Import Kubeconfig — browser-based file upload + context picker
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
                    options: '<input type="file" id="kubeconfig-file-input" accept=".yaml,.yml,.conf,.config,*" ' +
                             'style="padding: 12px; border: 2px dashed var(--border-color); border-radius: 8px; ' +
                             'width: 100%; cursor: pointer; background: var(--bg-color);" />'
                }
            ],
            primary_action_label: __('Parse Contexts'),
            primary_action: function() {
                const file_input = document.getElementById('kubeconfig-file-input');
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
    }
});


// ---------------------------------------------------------------------------
// Private helpers — context picker dialog
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
                const is_current = ctx.is_current ? '★' : '';
                const row_class = ctx.is_current ? 'font-weight-bold' : '';
                table_html += `
                    <tr data-idx="${idx}" class="context-row ${row_class}"
                        style="transition: background 0.15s;">
                        <td><code>${frappe.utils.escape_html(ctx.context_name)}</code></td>
                        <td>${frappe.utils.escape_html(ctx.cluster_name)}</td>
                        <td><code>${frappe.utils.escape_html(ctx.server)}</code></td>
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
                        fieldname: 'name_section',
                        fieldtype: 'Section Break',
                        label: __('Cluster Display Name'),
                        hidden: 1
                    },
                    {
                        fieldname: 'display_name_info',
                        fieldtype: 'HTML',
                        options: '<p class="text-muted">' +
                            __('Give this cluster a friendly name. The technical context name is stored separately.') +
                            '</p>',
                        hidden: 1
                    },
                    {
                        fieldname: 'display_name',
                        fieldtype: 'Data',
                        label: __('Display Name'),
                        description: __('e.g. "Production East", "Dev K3d", "Staging GKE"'),
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
                            if (!r.message) return;

                            // Hydrate the DocType
                            frm.set_value('kubeconfig', r.message);
                            frm.set_value('kubeconfig_context', ctx.context_name);
                            frm.set_value('cluster_name', display_name);

                            frm.dirty();
                            context_dialog.hide();

                            frappe.show_alert({
                                message: __('Context "{0}" imported as "{1}". Save the document to persist.', [ctx.context_name, display_name]),
                                indicator: 'green'
                            }, 5);
                        }
                    });
                }
            });

            context_dialog.show();

            // Row selection behavior — selecting a row reveals the display name field
            context_dialog.$wrapper.on('click', '.context-row', function() {
                const idx = parseInt($(this).data('idx'));
                selected_context = idx;

                const ctx = contexts[idx];

                // Highlight selected row
                context_dialog.$wrapper.find('.context-row').css('background', '');
                $(this).css('background', 'var(--highlight-color, #e8f4fd)');

                // Show and pre-fill the display name field
                context_dialog.fields_dict.name_section.df.hidden = 0;
                context_dialog.fields_dict.name_section.refresh();
                context_dialog.fields_dict.display_name_info.df.hidden = 0;
                context_dialog.fields_dict.display_name_info.refresh();
                context_dialog.fields_dict.display_name.df.hidden = 0;
                context_dialog.fields_dict.display_name.refresh();
                context_dialog.set_value('display_name', ctx.cluster_name);
            });

            // Pre-select the current context if one exists
            const current_idx = contexts.findIndex(ctx => ctx.is_current);
            if (current_idx >= 0) {
                selected_context = current_idx;
                const current_ctx = contexts[current_idx];

                context_dialog.$wrapper
                    .find(`.context-row[data-idx="${current_idx}"]`)
                    .css('background', 'var(--highlight-color, #e8f4fd)');

                // Show display name field pre-filled with the current context's cluster
                context_dialog.fields_dict.name_section.df.hidden = 0;
                context_dialog.fields_dict.name_section.refresh();
                context_dialog.fields_dict.display_name_info.df.hidden = 0;
                context_dialog.fields_dict.display_name_info.refresh();
                context_dialog.fields_dict.display_name.df.hidden = 0;
                context_dialog.fields_dict.display_name.refresh();
                context_dialog.set_value('display_name', current_ctx.cluster_name);
            }
        }
    });
}