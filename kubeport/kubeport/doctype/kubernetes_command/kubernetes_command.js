frappe.ui.form.on('Kubernetes Command', {
    execute_command: function(frm) {
        if (!frm.doc.cluster || !frm.doc.resource_type) {
            frappe.msgprint(__("Please select a cluster and a resource type."));
            return;
        }

        frappe.call({
            doc: frm.doc,
            method: 'execute_command',
            freeze: true,
            freeze_message: __('Querying cluster...'),
            callback: function(r) {
                if (!r.exc) {
                    frm.reload_doc();
                }
            }
        });
    }
});