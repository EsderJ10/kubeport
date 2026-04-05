frappe.ui.form.on('Kubernetes Manifest', {
    refresh: function(frm) {
        const status_map = {
            'Applied': 'green',
            'Failed': 'red',
            'In Progress': 'blue',
            'Degraded': 'yellow',
            'Draft': 'orange'
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
        }

        // Listen for realtime status updates from background jobs
        frappe.realtime.on('manifest_status_update', (data) => {
            if (data.manifest_name === frm.doc.name) {
                frm.reload_doc();
            }
        });
    },

    apply_manifest: function(frm) {
        if (frm.is_dirty()) {
            frappe.msgprint(__('Please, save the document before applying it.'));
            return;
        }

        frappe.call({
            doc: frm.doc,
            method: 'apply_manifest',
            callback: function(r) {
                if (!r.exc) frm.reload_doc();
            }
        });
    },

    delete_manifest: function(frm) {
        frappe.confirm('Are you sure you want to destroy the JSON resources of this manifest?',
            () => {
                frappe.call({
                    doc: frm.doc,
                    method: 'delete_manifest',
                    callback: function(r) {
                        if (!r.exc) frm.reload_doc();
                    }
                });
            }
        );
    }
});