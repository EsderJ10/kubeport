frappe.ui.form.on('Kubernetes Manifest', {
    refresh: function(frm) {
        if (frm.doc.status === 'Applied') {
            frm.page.set_indicator('Applied', 'green');
        } else if (frm.doc.status === 'Failed') {
            frm.page.set_indicator('Failed', 'red');
        } else {
            frm.page.set_indicator('Draft', 'orange');
        }
    },

    apply_manifest: function(frm) {
        if (frm.is_dirty()) {
            frappe.msgprint(__('Please, save the document before applying it.'));
            return;
        }

        frappe.call({
            doc: frm.doc,
            method: 'apply_manifest',
            freeze: true,
            freeze_message: __('Validating...'),
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
                    freeze: true,
                    freeze_message: __('Destroying infrastructure...'),
                    callback: function(r) {
                        if (!r.exc) frm.reload_doc();
                    }
                });
            }
        );
    }
});