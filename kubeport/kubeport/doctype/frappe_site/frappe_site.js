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

		const in_progress = frm.doc.status === 'In Progress';
		const has_job = !!frm.doc.creation_job_name;

		// Create button: hide while a Job is running; relabel once the site exists
		// so the only way to re-provision is via an explicit Force Create.
		frm.toggle_display('create_site_btn', !in_progress);
		if (frm.fields_dict.create_site_btn && frm.fields_dict.create_site_btn.$input) {
			const label = frm.doc.status === 'Active' ? __('Recreate Site (Force)') : __('Create Site');
			frm.fields_dict.create_site_btn.$input.text(label);
		}

		// Logs available only when a Job has been submitted for this site.
		frm.toggle_display('view_job_logs_btn', has_job);

		// Cancel only while a Job is running.
		frm.toggle_display('cancel_site_btn', in_progress);

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

		if (frm.doc.status === 'Active' && !frm.doc.force_create) {
			frappe.throw(
				__('This site is Active. Enable "Force Create" and save before recreating.')
			);
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
	},

	cancel_site_btn: function (frm) {
		frappe.confirm(
			__('Cancel site creation for "{0}"? The Kubernetes Job will be deleted.',
				[frm.doc.site_name]),
			() => {
				frappe.call({
					doc: frm.doc,
					method: 'cancel_site',
					callback: function (r) {
						if (!r.exc) frm.reload_doc();
					}
				});
			}
		);
	},

	view_job_logs_btn: function (frm) {
		frappe.call({
			method: 'kubeport.api.site.get_site_job_logs',
			args: { site_docname: frm.doc.name },
			freeze: true,
			freeze_message: __('Fetching job logs...'),
			callback: function (r) {
				const payload = r.message || {};
				const header = payload.job_name
					? __('Job: {0}', [payload.job_name])
					: __('No job has been submitted yet.');
				const body = payload.error
					? `<div class="text-muted">${frappe.utils.escape_html(payload.error)}</div>`
					: `<pre style="white-space: pre-wrap; max-height: 60vh; overflow: auto;">${
						frappe.utils.escape_html(payload.logs || __('(no logs yet)'))
					}</pre>`;
				const dialog = new frappe.ui.Dialog({
					title: __('Site Creation Job Logs'),
					size: 'large',
				});
				dialog.$body.html(`<p><strong>${header}</strong></p>${body}`);
				dialog.show();
			}
		});
	}
});
