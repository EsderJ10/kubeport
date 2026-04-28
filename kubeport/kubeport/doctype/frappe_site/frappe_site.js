frappe.ui.form.on('Frappe Site', {
	refresh: function (frm) {
		const status_map = {
			'Active': 'green',
			'Failed': 'red',
			'In Progress': 'blue',
			'Migrating': 'blue',
			'Deleting': 'red',
			'Draft': 'orange'
		};
		if (frm.doc.status) {
			frm.page.set_indicator(frm.doc.status, status_map[frm.doc.status] || 'grey');
		}

		const in_flight = ['In Progress', 'Migrating', 'Deleting'].includes(frm.doc.status);
		const has_job = !!frm.doc.operation_job_name;

		// Create button: hide while any operation is running; relabel once the
		// site exists so the only way to re-provision is via an explicit Force
		// Create.
		frm.toggle_display('create_site_btn', !in_flight);
		if (frm.fields_dict.create_site_btn && frm.fields_dict.create_site_btn.$input) {
			const label = frm.doc.status === 'Active' ? __('Recreate Site (Force)') : __('Create Site');
			frm.fields_dict.create_site_btn.$input.text(label);
		}

		// Migrate is only meaningful on a healthy site.
		frm.toggle_display('migrate_site_btn', frm.doc.status === 'Active');

		// Delete drops the bench-side site. Available from Active (the normal
		// path) and Failed rows that previously launched a Job (so a real site
		// may exist on the bench).
		const can_delete = frm.doc.status === 'Active' ||
			(frm.doc.status === 'Failed' && has_job);
		frm.toggle_display('delete_site_btn', can_delete);

		// Logs available whenever a Job has been submitted for this site
		// (creation, migrate, or delete).
		frm.toggle_display('view_job_logs_btn', has_job);

		// Cancel during any in-flight op; relabel by operation type.
		frm.toggle_display('cancel_site_btn', in_flight);
		if (in_flight && frm.fields_dict.cancel_site_btn && frm.fields_dict.cancel_site_btn.$input) {
			const cancel_labels = {
				'In Progress': __('Cancel Creation'),
				'Migrating': __('Cancel Migration'),
				'Deleting': __('Cancel Deletion'),
			};
			frm.fields_dict.cancel_site_btn.$input.text(cancel_labels[frm.doc.status] || __('Cancel'));
		}

		if (!frm.__frappe_site_status_listener_bound) {
			frm.__frappe_site_status_listener_bound = true;
			frappe.realtime.on('frappe_site_status_update', (data) => {
				if (data.site_docname !== frm.doc.name) return;
				if (data.status === 'Deleted') {
					// Reconciliation just removed this row; reload would 404.
					frappe.show_alert({
						message: __('Site "{0}" has been dropped from the bench.',
							[frm.doc.site_name]),
						indicator: 'green',
					});
					frappe.set_route('List', 'Frappe Site');
					return;
				}
				frm.reload_doc();
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

	migrate_site_btn: function (frm) {
		frappe.confirm(
			__('Run "bench migrate" on site "{0}"? The site will be briefly unavailable while schema migrations apply.',
				[frm.doc.site_name]),
			() => {
				frappe.call({
					doc: frm.doc,
					method: 'migrate_site',
					callback: function (r) {
						if (!r.exc) frm.reload_doc();
					}
				});
			}
		);
	},

	delete_site_btn: function (frm) {
		frappe.warn(
			__('Drop site "{0}"?', [frm.doc.site_name]),
			__('This runs "bench drop-site --no-backup --force". The site database and files will be deleted from the bench. This cannot be undone. The Frappe Site row will be removed automatically once the bench confirms the site is gone.'),
			() => {
				frappe.call({
					doc: frm.doc,
					method: 'delete_site',
					callback: function (r) {
						if (!r.exc) frm.reload_doc();
					}
				});
			},
			__('Drop Site'),
			true,
		);
	},

	cancel_site_btn: function (frm) {
		if (frm.doc.status === 'Migrating') {
			const dialog = new frappe.ui.Dialog({
				title: __('Cancel migration of "{0}"', [frm.doc.site_name]),
				fields: [
					{
						fieldtype: 'HTML',
						fieldname: 'warning',
						options: `
							<div class="alert alert-danger">
								<strong>${__('Destructive action')}.</strong>
								${__(
									'Cancelling a running migration can leave the database schema in a half-applied state ' +
									'with no automatic rollback. The site may need manual recovery.'
								)}
							</div>
							<p>${__('Type <code>CANCEL</code> below to enable the destructive action.')}</p>
						`,
					},
					{
						fieldtype: 'Data',
						fieldname: 'confirm',
						label: __('Type CANCEL to confirm'),
						reqd: 1,
					},
				],
				primary_action_label: __('Cancel migration'),
				primary_action(values) {
					if ((values.confirm || '').trim() !== 'CANCEL') {
						frappe.show_alert({
							message: __('Type CANCEL exactly to confirm.'),
							indicator: 'orange',
						});
						return;
					}
					dialog.hide();
					frappe.call({
						doc: frm.doc,
						method: 'cancel_site',
						args: { confirm_destructive: 1 },
						callback: function (r) {
							if (!r.exc) frm.reload_doc();
						}
					});
				},
			});
			dialog.show();

			const $btn = dialog.get_primary_btn();
			$btn.prop('disabled', true);
			dialog.fields_dict.confirm.$input.on('input', function () {
				const ok = ($(this).val() || '').trim() === 'CANCEL';
				$btn.prop('disabled', !ok);
			});
			return;
		}

		const op_labels = {
			'In Progress': __('Cancel site creation for "{0}"? The Kubernetes Job will be deleted.',
				[frm.doc.site_name]),
			'Deleting': __('Cancel deletion of "{0}"? The drop-site Job will be deleted; the site may already be partially dropped.',
				[frm.doc.site_name]),
		};
		const message = op_labels[frm.doc.status] || __('Cancel current operation?');
		frappe.confirm(
			message,
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
					title: __('Operation Job Logs'),
					size: 'large',
				});
				dialog.$body.html(`<p><strong>${header}</strong></p>${body}`);
				dialog.show();
			}
		});
	}
});
