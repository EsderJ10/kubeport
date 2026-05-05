frappe.ui.form.on('Frappe Site Backup', {
	refresh: function (frm) {
		const status_map = {
			'Pending': 'orange',
			'In Progress': 'blue',
			'Available': 'green',
			'Restoring': 'blue',
			'Failed': 'red',
		};
		if (frm.doc.status) {
			frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
		}

		// Auto-refresh the form when the backup row's status changes server-side
		// (worker submitted the Job, reconciliation finalized Available/Failed,
		// or a site cancellation cascaded a Failed). The site form already has
		// this listener; mirror it here so a user viewing the backup doesn't
		// see stale state.
		if (!frm.__frappe_site_backup_status_listener_bound) {
			frm.__frappe_site_backup_status_listener_bound = true;
			frappe.realtime.on('frappe_site_backup_status_update', (data) => {
				if (data.backup_docname && data.backup_docname !== frm.doc.name) return;
				frm.reload_doc();
			});
		}
	}
});
