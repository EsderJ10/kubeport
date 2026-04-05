frappe.ui.form.on('Helm Release', {
    refresh: function(frm) {
        const status_map = {
            'Deployed': 'green',
            'Failed': 'red',
            'In Progress': 'blue',
            'Degraded': 'yellow',
            'Draft': 'orange'
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
        }

        // Listen for realtime status updates from background jobs
        frappe.realtime.on('helm_status_update', (data) => {
            if (data.release_name === frm.doc.name) {
                frm.reload_doc();
            }
        });
    },

    deploy_release: function(frm) {
        if (frm.is_dirty()) {
            frappe.throw(__('Save the document before deploying.'));
        }

        frappe.call({
            doc: frm.doc,
            method: 'deploy_release',
            callback: function(r) {
                if (!r.exc) frm.reload_doc();
            }
        });
    },

    uninstall_release: function(frm) {
        frappe.confirm(__('Destroy this release and all associated resources?'),
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
    }
});