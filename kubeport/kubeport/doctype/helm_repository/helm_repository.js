frappe.ui.form.on('Helm Repository', {
    refresh: function(frm) {
        const status_map = {
            'Synced': 'green',
            'Error': 'red',
            'Syncing': 'blue',
            'Pending': 'orange'
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
        }

        if (!frm.is_new()) {
            _load_chart_count(frm);
        }

        _bind_repo_sync_listener(frm);
    },

    sync_charts: function(frm) {
        if (frm.is_dirty()) {
            frappe.throw(__('Save the document before syncing charts.'));
        }

        frappe.call({
            doc: frm.doc,
            method: 'sync_charts',
            callback: function(r) {
                if (!r.exc) frm.reload_doc();
            }
        });
    }
});

function _load_chart_count(frm) {
    frappe.call({
        method: 'frappe.client.get_count',
        args: {
            doctype: 'Helm Chart',
            filters: { repository: frm.doc.name }
        },
        callback: function(r) {
            if (r.message == null) {
                return;
            }

            const chart_count = Number(r.message) || 0;
            frm.set_intro(
                __('{0} Charts Discovered', [chart_count]),
                chart_count > 0 ? 'green' : 'orange'
            );
        }
    });
}

function _bind_repo_sync_listener(frm) {
    if (frm.__helm_repo_sync_listener_bound) {
        return;
    }

    frm.__helm_repo_sync_listener_bound = true;
    frappe.realtime.on('helm_repo_sync_update', (data) => {
        if (data.repo_name === frm.doc.name) {
            frm.reload_doc();
        }
    });
}
