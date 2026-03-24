frappe.ui.form.on('Kubernetes Command', {
    execute_command: function(frm) {
        if (!frm.doc.cluster || !frm.doc.command) {
            frappe.msgprint("Please, select a cluster and write a command.");
            return;
        }

        frappe.call({
            doc: frm.doc,
            method: 'execute_command',
            freeze: true,
            freeze_message: __('Executing in the cluster...'),
            callback: function(r) {
                if (!r.exc) {
                    frm.reload_doc(); // Recarga para mostrar el Output
                }
            }
        });
    }
}); 