frappe.ui.form.on('Kubernetes Command', {
    refresh: function(frm) {
        // Clear output field visually on fresh documents
        if (frm.is_new()) {
            frm.set_value('output', '');
        }
    },

    cluster: function(frm) {
        // When the cluster changes, refresh namespace suggestions
        frm.set_value('namespace', 'default');
    },

    execute_command: function(frm) {
        if (!frm.doc.cluster || !frm.doc.resource_type) {
            frappe.msgprint(__('Please select a cluster and a resource type.'));
            return;
        }

        if (frm.is_dirty()) {
            frappe.msgprint(__('Please save the document before executing.'));
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

// Namespace autocomplete — fetches live namespaces from the selected cluster
frappe.ui.form.on('Kubernetes Command', {
    setup: function(frm) {
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