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

        // Contextual note: this DocType currently applies raw manifests, not actual Helm charts
        if (!frm.is_new()) {
            frm.set_intro(
                __('Note: This release deploys raw Kubernetes manifests from the Values field. Actual Helm chart integration is planned for a future release.'),
                'yellow'
            );
        }

        // Listen for realtime status updates from background jobs
        frappe.realtime.on('helm_status_update', (data) => {
            if (data.release_name === frm.doc.name) {
                frm.reload_doc();
            }
        });
    },

    cluster: function(frm) {
        // When the cluster changes, refresh namespace suggestions
        frm.set_value('namespace', 'default');
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

// Namespace autocomplete — fetches live namespaces from the selected cluster
frappe.ui.form.on('Helm Release', 'namespace', function() {});
frappe.ui.form.on('Helm Release', {
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