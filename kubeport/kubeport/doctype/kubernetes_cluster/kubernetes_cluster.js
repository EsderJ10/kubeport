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

        // Show a contextual hint based on auth method
        if (frm.doc.auth_method === 'In-Cluster') {
            frm.set_intro(
                __('In-Cluster auth auto-detects credentials from the Kubernetes pod environment. No manual configuration needed.'),
                'blue'
            );
        }
    },

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