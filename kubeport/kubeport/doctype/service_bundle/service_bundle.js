frappe.ui.form.on('Service Bundle', {
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
        frappe.realtime.on('service_bundle_status_update', (data) => {
            if (data.bundle_name === frm.doc.name) {
                frm.reload_doc();
            }
        });
    },

    cluster: function(frm) {
        frm.set_value('namespace', 'default');
    },

    apply_bundle: function(frm) {
        if (frm.is_dirty()) {
            frappe.throw(__('Save the document before deploying.'));
        }

        frappe.call({
            doc: frm.doc,
            method: 'apply_bundle',
            callback: function(r) {
                if (!r.exc) frm.reload_doc();
            }
        });
    },

    delete_bundle: function(frm) {
        frappe.confirm(__('Delete all resources deployed by this bundle?'),
            () => {
                frappe.call({
                    doc: frm.doc,
                    method: 'delete_bundle',
                    callback: function(r) {
                        if (!r.exc) frm.reload_doc();
                    }
                });
            }
        );
    }
});

// Namespace autocomplete — fetches live namespaces from the selected cluster
frappe.ui.form.on('Service Bundle', {
    setup: function(frm) {
        frm.set_query('namespace', function() {
            return {};
        });

        frm.fields_dict.namespace.get_data = function(txt) {
            if (!frm.doc.cluster) return [];

            return new Promise((resolve) => {
                frappe.call({
                    method: 'kubeport.api.get_cluster_namespaces',
                    args: { cluster_name: frm.doc.cluster },
                    callback: function(r) {
                        if (r.message) {
                            const results = r.message
                                .filter(ns => !txt || ns.toLowerCase().includes(txt.toLowerCase()))
                                .map(ns => ({ label: ns, value: ns }));
                            resolve(results);
                        } else {
                            resolve([]);
                        }
                    }
                });
            });
        };
    }
});
