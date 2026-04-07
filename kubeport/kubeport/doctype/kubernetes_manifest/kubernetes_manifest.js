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

    cluster: function(frm) {
        // When the cluster changes, refresh namespace suggestions
        frm.set_value('namespace', 'default');
    },

    apply_manifest: function(frm) {
        if (frm.is_dirty()) {
            frappe.msgprint(__('Please save the document before applying it.'));
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
        frappe.confirm(__('Are you sure you want to destroy the resources of this manifest?'),
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

// Namespace autocomplete — fetches live namespaces from the selected cluster
frappe.ui.form.on('Kubernetes Manifest', {
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