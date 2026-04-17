frappe.ui.form.on('Frappe Site', {
	refresh: function (frm) {
		const status_map = {
			'Active': 'green',
			'Failed': 'red',
			'In Progress': 'blue',
			'Draft': 'orange'
		};
		if (frm.doc.status) {
			frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
		}

		if (!frm.__frappe_site_status_listener_bound) {
			frm.__frappe_site_status_listener_bound = true;
			frappe.realtime.on('frappe_site_status_update', (data) => {
				if (data.site_docname === frm.doc.name) {
					frm.reload_doc();
				}
			});
		}
	},

	create_site_btn: function (frm) {
		if (frm.is_dirty()) {
			frappe.throw(__('Save the document before creating the site.'));
		}

		frappe.confirm(
			__('Create site "{0}" on bench "{1}"?',
				[frm.doc.site_name, frm.doc.bench_release]),
			() => {
				frappe.call({
					doc: frm.doc,
					method: 'create_site',
					callback: function (r) {
						if (!r.exc) frm.reload_doc();
					}
				});
			}
		);
	}
});
