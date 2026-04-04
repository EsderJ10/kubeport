frappe.ui.form.on('Helm Release', {
    refresh: function(frm) {
        const status_map = { 'Deployed': 'green', 'Failed': 'red', 'Draft': 'orange' };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
        }
    },

    deploy_release: function(frm) {
        if (frm.is_dirty()) {
            frappe.throw(__('Guarda el documento antes de desplegar.'));
        }

        frappe.call({
            doc: frm.doc,
            method: 'deploy_release',
            freeze: true,
            freeze_message: __('Ejecutando Helm... esto puede tardar unos minutos en clústeres remotos.'),
            callback: function(r) {
                if (!r.exc) frm.reload_doc();
            }
        });
    },

    uninstall_release: function(frm) {
        frappe.confirm(__('¿Destruir esta Release y todos sus datos asociados?'),
            () => {
                frappe.call({
                    doc: frm.doc,
                    method: 'uninstall_release',
                    freeze: true,
                    freeze_message: __('Desinstalando paquete...'),
                    callback: function(r) {
                        if (!r.exc) frm.reload_doc();
                    }
                });
            }
        );
    }
});