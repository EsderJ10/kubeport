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
	}
});
