frappe.ui.form.on('Helm Repository', {
    refresh: function(frm) {
        const status_map = {
            'Synced': 'green',
            'Error': 'red',
            'Syncing': 'blue',
            'Pending': 'orange'
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
        }

        // Show linked chart count
        if (!frm.is_new()) {
            frappe.call({
                method: 'frappe.client.get_count',
                args: {
                    doctype: 'Helm Chart',
                    filters: { repository: frm.doc.name }
                },
                callback: function(r) {
                    if (r.message) {
                        frm.dashboard.add_indicator(
                            __('{0} Charts Discovered', [r.message]),
                            r.message > 0 ? 'green' : 'orange'
                        );
                    }
                }
            });
        }

        // Listen for realtime sync updates
        frappe.realtime.on('helm_repo_sync_update', (data) => {
            if (data.repo_name === frm.doc.name) {
                frm.reload_doc();
            }
        });
    },

    sync_charts: function(frm) {
        if (frm.is_dirty()) {
            frappe.throw(__('Save the document before syncing charts.'));
        }

        frappe.call({
            doc: frm.doc,
            method: 'sync_charts',
            callback: function(r) {
                if (!r.exc) frm.reload_doc();
            }
        });
    }
});
