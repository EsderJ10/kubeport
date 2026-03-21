frappe.ui.form.on('Kubernetes Cluster', {
    test_connection: function(frm) {
        frappe.call({
            doc: frm.doc,
            method: 'test_connection', // Llama a la función de Python
            freeze: true,
            freeze_message: 'Conectando con el clúster...',
            callback: function(r) {
                if (!r.exc) {
                    frm.reload_doc(); // Recarga para ver el nuevo Status
                }
            }
        });
    }
});