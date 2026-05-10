frappe.ui.form.on('Frappe Site', {
	refresh: function (frm) {
		kubeport_site_close_observability_panel(frm);
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
		frm.toggle_display('backup_site_btn', frm.doc.status === 'Active');

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
				if (frm.__kubeport_site_observability_state) {
					kubeport_render_site_health(frm);
					kubeport_site_refresh_active_observability_panel(frm);
				} else {
					frm.reload_doc();
				}
			});
			frappe.realtime.on('frappe_site_backup_status_update', (data) => {
				if (data.site_docname && data.site_docname !== frm.doc.name) return;
				frm.reload_doc();
			});
		}

		render_backups(frm);
		kubeport_render_site_health(frm);
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

	backup_site_btn: function (frm) {
		if (frm.is_dirty()) {
			frappe.throw(__('Save the document before backing up the site.'));
		}
		frappe.confirm(
			__('Create a backup of site "{0}"?', [frm.doc.site_name]),
			() => {
				frappe.call({
					doc: frm.doc,
					method: 'backup_site',
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

function render_backups(frm) {
	const field = frm.fields_dict.backups_html;
	if (!field) return;
	if (frm.is_new()) {
		field.$wrapper.empty();
		return;
	}

	frappe.call({
		method: 'kubeport.api.site.list_site_backups',
		args: { site_docname: frm.doc.name },
		callback: function (r) {
			const rows = r.message || [];
			if (!rows.length) {
				field.$wrapper.html('<div class="text-muted">' + __('No backups') + '</div>');
				return;
			}
			const html = rows.map((row) => {
				const status_class = {
					'Available': 'green',
					'Failed': 'red',
					'In Progress': 'blue',
					'Restoring': 'blue',
					'Pending': 'orange',
				}[row.status] || 'grey';
				const size = row.size_bytes ? format_bytes(row.size_bytes) : '';
				const restore = row.status === 'Available'
					? `<button class="btn btn-xs btn-primary restore-backup" data-name="${frappe.utils.escape_html(row.name)}">${__('Restore')}</button>`
					: '';
				const logs = ['In Progress', 'Restoring', 'Failed'].includes(row.status)
					? `<button class="btn btn-xs btn-default backup-logs" data-name="${frappe.utils.escape_html(row.name)}">${__('Logs')}</button>`
					: '';
				return `
					<tr>
						<td><a href="/app/frappe-site-backup/${encodeURIComponent(row.name)}">${frappe.utils.escape_html(row.backup_name || row.name)}</a></td>
						<td><span class="indicator-pill ${status_class}">${frappe.utils.escape_html(row.status || '')}</span></td>
						<td>${frappe.utils.escape_html(size)}</td>
						<td>${frappe.utils.escape_html(row.completed_at || row.started_at || '')}</td>
						<td class="text-right">${restore} ${logs}</td>
					</tr>
				`;
			}).join('');
			field.$wrapper.html(`
				<div class="table-responsive">
					<table class="table table-bordered table-hover">
						<thead>
							<tr>
								<th>${__('Backup')}</th>
								<th>${__('Status')}</th>
								<th>${__('Size')}</th>
								<th>${__('Time')}</th>
								<th></th>
							</tr>
						</thead>
						<tbody>${html}</tbody>
					</table>
				</div>
			`);
			field.$wrapper.find('.restore-backup').on('click', function () {
				show_restore_dialog(frm, $(this).data('name'));
			});
			field.$wrapper.find('.backup-logs').on('click', function () {
				show_backup_logs($(this).data('name'));
			});
		}
	});
}

function show_restore_dialog(frm, backup_docname) {
	const expected = `RESTORE ${frm.doc.site_name}`;
	const dialog = new frappe.ui.Dialog({
		title: __('Restore "{0}"', [frm.doc.site_name]),
		fields: [
			{
				fieldtype: 'HTML',
				fieldname: 'warning',
				options: `<div class="alert alert-danger">${__('This overwrites the current database and files.')}</div>`,
			},
			{
				fieldtype: 'Data',
				fieldname: 'confirm',
				label: __('Type {0}', [expected]),
				reqd: 1,
			},
		],
		primary_action_label: __('Restore'),
		primary_action(values) {
			if ((values.confirm || '').trim() !== expected) {
				frappe.show_alert({
					message: __('Confirmation text does not match.'),
					indicator: 'orange',
				});
				return;
			}
			dialog.hide();
			frappe.call({
				doc: frm.doc,
				method: 'restore_site',
				args: {
					backup_docname: backup_docname,
					confirm_destructive: 1,
				},
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
		$btn.prop('disabled', ($(this).val() || '').trim() !== expected);
	});
}

function show_backup_logs(backup_docname) {
	frappe.call({
		method: 'kubeport.api.site.get_site_backup_job_logs',
		args: { backup_docname: backup_docname },
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
				title: __('Backup Job Logs'),
				size: 'large',
			});
			dialog.$body.html(`<p><strong>${header}</strong></p>${body}`);
			dialog.show();
		}
	});
}

function format_bytes(value) {
	const bytes = Number(value || 0);
	if (!bytes) return '';
	const units = ['B', 'KB', 'MB', 'GB', 'TB'];
	let size = bytes;
	let index = 0;
	while (size >= 1024 && index < units.length - 1) {
		size = size / 1024;
		index += 1;
	}
	return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

// Bench workload-readiness drilldown.  The site is hosted on its bench
// release; we surface that release's per-resource readiness here, scoped to
// this site's form, and reuse the per-resource Logs/Events/Rollout panels
// from the Helm Release form via site-scoped API endpoints.
function kubeport_render_site_health(frm) {
	if (!frm.doc.name || frm.is_new()) return;

	const $wrapper = kubeport_site_get_health_wrapper(frm);
	if (!$wrapper) return;

	const HEALTHY_STATUSES = ['Active', 'Migrating', 'In Progress', 'Failed'];
	if (!frm.doc.bench_release) {
		$wrapper.html(
			`<div class="text-muted small">${__('Bench health will appear once a bench release is linked.')}</div>`
		);
		return;
	}
	if (!HEALTHY_STATUSES.includes(frm.doc.status)) {
		$wrapper.html(
			`<div class="text-muted small">${__('Bench workload readiness will appear once the site is created.')}</div>`
		);
		return;
	}

	$wrapper.html(`<div class="text-muted small">${__('Loading bench readiness…')}</div>`);

	const request_id = (frm.__frappe_site_health_request_id || 0) + 1;
	frm.__frappe_site_health_request_id = request_id;

	frappe.call({
		doc: frm.doc,
		method: 'get_site_health',
	}).then((r) => {
		if (request_id !== frm.__frappe_site_health_request_id) return;
		if (r && !r.exc) {
			const payload = r.message || {};
			kubeport_site_paint_health_rows(frm, $wrapper, payload.rows || [], payload.error || '');
		} else {
			$wrapper.html(
				`<div class="text-muted small">${__('Could not fetch bench readiness.')}</div>`
			);
		}
	}, () => {
		if (request_id !== frm.__frappe_site_health_request_id) return;
		$wrapper.html(
			`<div class="text-muted small">${__('Could not fetch bench readiness.')}</div>`
		);
	});
}

function kubeport_site_get_health_wrapper(frm) {
	const detail_field = frm.fields_dict.site_health_detail;
	if (!detail_field || !detail_field.$wrapper) return null;

	let $panel = detail_field.$wrapper.find('.kubeport-site-health');
	if (!$panel.length) {
		$panel = $(
			`<div class="kubeport-site-health"
				  style="margin-top: 12px; padding: 8px;
						 border: 1px solid var(--border-color);
						 border-radius: 4px;">
				<div class="kubeport-site-health-header"
					 style="display: flex; justify-content: space-between;
							align-items: center; margin-bottom: 8px;">
					<strong>${__('Bench Workload Readiness')}</strong>
					<button class="btn btn-xs btn-default kubeport-site-health-refresh">
						${__('Refresh')}
					</button>
				</div>
				<div class="kubeport-site-health-body"></div>
			</div>`
		);
		detail_field.$wrapper.append($panel);
		$panel.find('.kubeport-site-health-refresh').on('click', () => {
			kubeport_render_site_health(frm);
		});
	}
	return $panel.find('.kubeport-site-health-body');
}

function kubeport_site_paint_health_rows(frm, $body, rows, error) {
	const error_html = error
		? `<div class="text-muted small" style="margin-bottom: 8px;">
				${__('Readiness check error:')} ${frappe.utils.escape_html(error)}
		   </div>`
		: '';

	if (!rows.length) {
		$body.html(
			`${error_html}
			 <div class="text-muted small">${__('No workload resources found in this bench release.')}</div>`
		);
		return;
	}

	const lines = rows.map((row) => {
		const dot = row.ready
			? '<span style="color: var(--green-500);">●</span>'
			: '<span style="color: var(--red-500);">●</span>';
		const reason = row.ready
			? (row.message || __('ready'))
			: `${row.reason}${row.message ? ' - ' + row.message : ''}`;
		const actions = row.ready ? '' : kubeport_site_render_health_actions(row);
		return `
			<div style="display: grid;
						grid-template-columns: 16px 110px 1fr 1.4fr 190px;
						gap: 8px; padding: 4px 0;
						border-bottom: 1px solid var(--border-color);
						font-size: 12px;">
				<div>${dot}</div>
				<div><code>${frappe.utils.escape_html(row.kind)}</code></div>
				<div style="word-break: break-all;">
					${frappe.utils.escape_html(row.name)}
				</div>
				<div class="text-muted">${frappe.utils.escape_html(reason)}</div>
				<div>${actions}</div>
			</div>
		`;
	}).join('');
	$body.html(`${error_html}${lines}`);
	$body.find('.kubeport-site-health-action').on('click', function () {
		const $button = $(this);
		const row = {
			kind: $button.attr('data-kind') || '',
			name: $button.attr('data-name') || '',
			namespace: $button.attr('data-namespace') || '',
			pod_count: cint($button.attr('data-pod-count') || 0),
		};
		const action = $button.attr('data-action');
		if (action === 'logs') {
			kubeport_site_open_observability_panel(frm, 'logs', row);
		} else if (action === 'events') {
			kubeport_site_open_observability_panel(frm, 'events', row);
		} else if (action === 'rollout') {
			kubeport_site_open_observability_panel(frm, 'rollout', row);
		}
	});
}

function kubeport_site_render_health_actions(row) {
	const kind = frappe.utils.escape_html(row.kind || '');
	const name = frappe.utils.escape_html(row.name || '');
	const namespace = frappe.utils.escape_html(row.namespace || '');
	const pod_count = cint(row.pod_count || 0);
	const log_kinds = ['Deployment', 'StatefulSet', 'DaemonSet', 'Pod'];
	const rollout_kinds = ['Deployment', 'StatefulSet', 'DaemonSet'];
	let html = '<div style="display: flex; gap: 4px; flex-wrap: wrap;">';
	if (log_kinds.includes(row.kind)) {
		html += `
			<button class="btn btn-xs btn-default kubeport-site-health-action"
					data-action="logs" data-kind="${kind}" data-name="${name}"
					data-namespace="${namespace}" data-pod-count="${pod_count}">
				${__('Logs')}
			</button>`;
	}
	html += `
		<button class="btn btn-xs btn-default kubeport-site-health-action"
				data-action="events" data-kind="${kind}" data-name="${name}"
				data-namespace="${namespace}" data-pod-count="${pod_count}">
			${__('Events')}
		</button>`;
	if (rollout_kinds.includes(row.kind)) {
		html += `
			<button class="btn btn-xs btn-default kubeport-site-health-action"
					data-action="rollout" data-kind="${kind}" data-name="${name}"
					data-namespace="${namespace}" data-pod-count="${pod_count}">
				${__('Rollout')}
			</button>`;
	}
	html += '</div>';
	return html;
}

function kubeport_site_open_observability_panel(frm, type, row) {
	frm.__kubeport_site_observability_panel_id = cint(frm.__kubeport_site_observability_panel_id || 0) + 1;
	frm.__kubeport_site_observability_state = {
		panel_id: frm.__kubeport_site_observability_panel_id,
		type,
		row,
		values: {
			tail_lines: '200',
			pod_name: '',
			container: '',
			previous: 0,
			sort_key: 'last_seen',
			sort_dir: 'desc'
		},
		rows: [],
		error: '',
		log_payload: null,
		request_id: 0
	};
	kubeport_site_render_observability_shell(frm);
	kubeport_site_refresh_active_observability_panel(frm);
}

function kubeport_site_refresh_active_observability_panel(frm) {
	const state = frm.__kubeport_site_observability_state;
	if (!state) return;

	if (state.type === 'logs') {
		kubeport_site_read_log_values(frm);
		kubeport_site_render_logs_panel(frm);
	} else if (state.type === 'events') {
		kubeport_site_fetch_resource_events(frm);
	} else if (state.type === 'rollout') {
		kubeport_site_fetch_resource_rollout(frm);
	}
}

function kubeport_site_render_observability_shell(frm) {
	const state = frm.__kubeport_site_observability_state;
	if (!state) return;

	const $panel = kubeport_site_get_observability_panel(frm);
	if (!$panel) return;

	const title_map = {
		logs: __('Logs'),
		events: __('Events'),
		rollout: __('Rollout')
	};
	const title = `${title_map[state.type] || ''}: ${state.row.kind}/${state.row.name}`;
	$panel.html(`
		<div class="kubeport-site-observability-header"
			 style="display: flex; justify-content: space-between; gap: 8px;
					align-items: center; margin-bottom: 8px;">
			<strong>${frappe.utils.escape_html(title)}</strong>
			<button class="btn btn-xs btn-default kubeport-site-observability-close">
				${__('Close')}
			</button>
		</div>
		<div class="kubeport-site-observability-body"></div>
	`);
	$panel.find('.kubeport-site-observability-close').on('click', () => {
		kubeport_site_close_observability_panel(frm);
	});
}

function kubeport_site_get_observability_panel(frm) {
	const detail_field = frm.fields_dict.site_health_detail;
	if (!detail_field || !detail_field.$wrapper) return null;

	let $panel = detail_field.$wrapper.find('.kubeport-site-observability-panel');
	if (!$panel.length) {
		$panel = $(`
			<div class="kubeport-site-observability-panel"
				 style="margin-top: 12px; padding: 8px;
						border: 1px solid var(--border-color);
						border-radius: 4px;">
			</div>
		`);
		detail_field.$wrapper.append($panel);
	}
	return $panel;
}

function kubeport_site_close_observability_panel(frm) {
	frm.__kubeport_site_observability_state = null;
	const detail_field = frm.fields_dict && frm.fields_dict.site_health_detail;
	if (detail_field && detail_field.$wrapper) {
		detail_field.$wrapper.find('.kubeport-site-observability-panel').remove();
	}
}

function kubeport_site_create_observability_request_token(frm) {
	const state = frm.__kubeport_site_observability_state;
	if (!state) return null;

	state.request_id = cint(state.request_id || 0) + 1;
	return {
		panel_id: state.panel_id,
		request_id: state.request_id,
		type: state.type || '',
		kind: (state.row && state.row.kind) || '',
		name: (state.row && state.row.name) || '',
		namespace: (state.row && state.row.namespace) || ''
	};
}

function kubeport_site_observability_request_is_active(frm, token) {
	const state = frm.__kubeport_site_observability_state;
	if (!state || !token) return false;

	const row = state.row || {};
	return cint(state.panel_id || 0) === cint(token.panel_id || 0)
		&& cint(state.request_id || 0) === cint(token.request_id || 0)
		&& (state.type || '') === token.type
		&& (row.kind || '') === token.kind
		&& (row.name || '') === token.name
		&& (row.namespace || '') === token.namespace;
}

function kubeport_site_render_logs_panel(frm) {
	const state = frm.__kubeport_site_observability_state;
	const $body = kubeport_site_get_observability_body(frm);
	if (!state || !$body) return;

	const values = state.values;
	$body.html(`
		<div class="kubeport-site-log-controls"
			 style="display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end;
					margin-bottom: 8px;">
			<label class="control-label" style="margin-bottom: 0;">
				${__('Tail Lines')}
				<select class="form-control input-xs kubeport-site-log-tail" style="width: 92px;">
					${['50', '200', '500', '2000'].map((option) => `
						<option value="${option}" ${values.tail_lines === option ? 'selected' : ''}>${option}</option>
					`).join('')}
				</select>
			</label>
			<label class="control-label kubeport-site-log-pod-control" style="margin-bottom: 0; display: none;">
				${__('Pod')}
				<select class="form-control input-xs kubeport-site-log-pod" style="min-width: 220px;"></select>
			</label>
			<label class="control-label" style="margin-bottom: 0;">
				${__('Container')}
				<input class="form-control input-xs kubeport-site-log-container"
					   style="width: 180px;" value="${frappe.utils.escape_html(values.container || '')}">
			</label>
			<label class="checkbox" style="margin: 0;">
				<input type="checkbox" class="kubeport-site-log-previous" ${values.previous ? 'checked' : ''}>
				${__('Previous container')}
			</label>
			<button class="btn btn-xs btn-default kubeport-site-log-refresh">${__('Refresh')}</button>
		</div>
		<div class="kubeport-site-log-result"></div>
	`);
	$body.find('.kubeport-site-log-refresh').on('click', () => kubeport_site_fetch_resource_logs(frm));
	$body.find('.kubeport-site-log-tail, .kubeport-site-log-previous').on('change', () => {
		kubeport_site_fetch_resource_logs(frm);
	});
	$body.find('.kubeport-site-log-pod').on('change', () => {
		kubeport_site_read_log_values(frm);
		kubeport_site_fetch_resource_logs(frm);
	});
	$body.find('.kubeport-site-log-container').on('keydown', (event) => {
		if (event.key === 'Enter') kubeport_site_fetch_resource_logs(frm);
	});
	kubeport_site_fetch_resource_logs(frm);
}

function kubeport_site_fetch_resource_logs(frm) {
	const state = frm.__kubeport_site_observability_state;
	const $body = kubeport_site_get_observability_body(frm);
	if (!state || !$body) return;

	kubeport_site_read_log_values(frm);
	const values = state.values;
	const $target = $body.find('.kubeport-site-log-result');
	const token = kubeport_site_create_observability_request_token(frm);
	$target.html(`<div class="text-muted small">${__('Loading logs…')}</div>`);
	frappe.call({
		method: 'kubeport.api.observability.get_site_resource_logs',
		args: {
			site_docname: frm.doc.name,
			kind: state.row.kind,
			name: state.row.name,
			namespace: state.row.namespace || null,
			pod_name: values.pod_name || null,
			container: values.container || null,
			tail_lines: cint(values.tail_lines || 200),
			previous: cint(values.previous || 0) ? 1 : 0
		}
	}).then((r) => {
		if (!kubeport_site_observability_request_is_active(frm, token)) return;
		if (r.exc) {
			$target.html(`<div class="text-muted small">${__('Could not fetch logs.')}</div>`);
			return;
		}
		const payload = r.message || {};
		state.log_payload = payload;
		kubeport_site_sync_log_pod_select(frm, payload);
		$target.html(kubeport_site_render_logs_payload(payload));
	}, () => {
		if (!kubeport_site_observability_request_is_active(frm, token)) return;
		$target.html(`<div class="text-muted small">${__('Could not fetch logs.')}</div>`);
	});
}

function kubeport_site_read_log_values(frm) {
	const state = frm.__kubeport_site_observability_state;
	const $body = kubeport_site_get_observability_body(frm);
	if (!state || !$body) return;

	state.values.tail_lines = String($body.find('.kubeport-site-log-tail').val() || state.values.tail_lines || '200');
	state.values.pod_name = String($body.find('.kubeport-site-log-pod').val() || state.values.pod_name || '');
	state.values.container = String($body.find('.kubeport-site-log-container').val() || '');
	state.values.previous = $body.find('.kubeport-site-log-previous').is(':checked') ? 1 : 0;
}

function kubeport_site_sync_log_pod_select(frm, payload) {
	const state = frm.__kubeport_site_observability_state;
	const $body = kubeport_site_get_observability_body(frm);
	if (!state || !$body) return;

	const pods = payload.pods || [];
	const options = pods
		.map(pod => pod.name || '')
		.filter(Boolean);
	const $select = $body.find('.kubeport-site-log-pod');
	$select.html(options.map((option) => {
		const selected = option === payload.selected_pod ? 'selected' : '';
		return `<option value="${frappe.utils.escape_html(option)}" ${selected}>
			${frappe.utils.escape_html(option)}
		</option>`;
	}).join(''));
	$body.find('.kubeport-site-log-pod-control').toggle(options.length > 1);

	const selected = payload.selected_pod || '';
	if (selected && options.includes(selected)) {
		state.values.pod_name = selected;
	}
}

function kubeport_site_render_logs_payload(payload) {
	const pods = payload.pods || [];
	const logs_by_pod = payload.logs_by_pod || {};
	const errors_by_pod = payload.errors_by_pod || {};
	const selected_pod = payload.selected_pod || (pods[0] && pods[0].name) || '';
	let html = kubeport_site_render_panel_error(payload.error || '');
	if (!pods.length) {
		return html || `<div class="text-muted small">${__('No pods were found for this resource.')}</div>`;
	}

	const pod = pods.find(row => row.name === selected_pod) || pods[0];
	const pod_name = pod.name || '';
	const containers = (pod.container_names || []).join(', ');
	const status = `${pod.phase || __('Unknown')} · ${__('restarts')}: ${pod.restart_count || 0}`;
	html += `
		<div style="margin-bottom: 12px;">
			<div style="display: flex; justify-content: space-between; gap: 8px; margin-bottom: 4px;">
				<strong>${frappe.utils.escape_html(pod_name)}</strong>
				<span class="text-muted small">${frappe.utils.escape_html(status)}</span>
			</div>
			${containers ? `<div class="text-muted small" style="margin-bottom: 4px;">
				${__('Containers')}: ${frappe.utils.escape_html(containers)}
			</div>` : ''}
			${errors_by_pod[pod_name] ? `<div class="text-muted small" style="margin-bottom: 4px;">
				${frappe.utils.escape_html(errors_by_pod[pod_name])}
			</div>` : ''}
			<pre style="max-height: 420px; overflow: auto; white-space: pre-wrap;
						background: var(--fg-color); color: var(--text-color);
						border: 1px solid var(--border-color); border-radius: 4px;
						padding: 8px; font-size: 12px;">${frappe.utils.escape_html(logs_by_pod[pod_name] || '')}</pre>
		</div>`;
	return html;
}

function kubeport_site_fetch_resource_events(frm) {
	const state = frm.__kubeport_site_observability_state;
	const $body = kubeport_site_get_observability_body(frm);
	if (!state || !$body) return;

	$body.html(`
		<div style="display: flex; justify-content: flex-end; margin-bottom: 8px;">
			<button class="btn btn-xs btn-default kubeport-site-events-refresh">${__('Refresh')}</button>
		</div>
		<div class="kubeport-site-events-result"></div>
	`);
	$body.find('.kubeport-site-events-refresh').on('click', () => kubeport_site_fetch_resource_events(frm));
	const $target = $body.find('.kubeport-site-events-result');
	const token = kubeport_site_create_observability_request_token(frm);
	$target.html(`<div class="text-muted small">${__('Loading events…')}</div>`);
	frappe.call({
		method: 'kubeport.api.observability.get_site_resource_events',
		args: {
			site_docname: frm.doc.name,
			kind: state.row.kind,
			name: state.row.name,
			namespace: state.row.namespace || null,
			limit: 20
		}
	}).then((r) => {
		if (!kubeport_site_observability_request_is_active(frm, token)) return;
		if (r.exc) {
			$target.html(kubeport_site_render_panel_error(__('Could not fetch events.')));
			return;
		}
		const payload = kubeport_site_normalize_observability_rows(r.message);
		state.rows = payload.rows;
		state.error = payload.error;
		kubeport_site_render_events_result(frm);
	}, () => {
		if (!kubeport_site_observability_request_is_active(frm, token)) return;
		$target.html(kubeport_site_render_panel_error(__('Could not fetch events.')));
	});
}

function kubeport_site_render_events_result(frm) {
	const state = frm.__kubeport_site_observability_state;
	const $body = kubeport_site_get_observability_body(frm);
	if (!state || !$body) return;

	const $target = $body.find('.kubeport-site-events-result');
	$target.html(kubeport_site_render_events(state.rows || [], state.values, state.error || ''));
	$target.find('.kubeport-site-events-sort').on('click', function () {
		const key = $(this).attr('data-sort-key') || 'last_seen';
		if (state.values.sort_key === key) {
			state.values.sort_dir = state.values.sort_dir === 'asc' ? 'desc' : 'asc';
		} else {
			state.values.sort_key = key;
			state.values.sort_dir = 'desc';
		}
		kubeport_site_render_events_result(frm);
	});
}

function kubeport_site_render_events(rows, values, error) {
	if (!rows.length) {
		return kubeport_site_render_panel_error(error)
			|| `<div class="text-muted small">${__('No scoped events were returned for this resource.')}</div>`;
	}
	rows = kubeport_site_sort_rows(rows, values.sort_key || 'last_seen', values.sort_dir || 'desc');
	let html = kubeport_site_render_panel_error(error);
	html += '<div class="table-responsive"><table class="table table-bordered" style="margin-bottom: 0;">';
	html += `<thead><tr>
		<th><button class="btn btn-xs btn-link kubeport-site-events-sort" data-sort-key="type">${__('Type')}</button></th>
		<th><button class="btn btn-xs btn-link kubeport-site-events-sort" data-sort-key="reason">${__('Reason')}</button></th>
		<th><button class="btn btn-xs btn-link kubeport-site-events-sort" data-sort-key="count">${__('Count')}</button></th>
		<th><button class="btn btn-xs btn-link kubeport-site-events-sort" data-sort-key="last_seen">${__('Age')}</button></th>
		<th>${__('Message')}</th>
	</tr></thead><tbody>`;
	rows.forEach((row) => {
		const message = row.message || '';
		const timestamp = row.last_seen || row.first_seen || '';
		html += `<tr>
			<td>${frappe.utils.escape_html(row.type || '')}</td>
			<td>${frappe.utils.escape_html(row.reason || '')}</td>
			<td>${frappe.utils.escape_html(String(row.count || 0))}</td>
			<td title="${frappe.utils.escape_html(timestamp)}">
				${frappe.utils.escape_html(kubeport_site_format_age(timestamp))}
			</td>
			<td style="word-break: break-word;" title="${frappe.utils.escape_html(message)}">
				${frappe.utils.escape_html(message)}
			</td>
		</tr>`;
	});
	html += '</tbody></table></div>';
	return html;
}

function kubeport_site_fetch_resource_rollout(frm) {
	const state = frm.__kubeport_site_observability_state;
	const $body = kubeport_site_get_observability_body(frm);
	if (!state || !$body) return;

	$body.html(`
		<div style="display: flex; justify-content: flex-end; margin-bottom: 8px;">
			<button class="btn btn-xs btn-default kubeport-site-rollout-refresh">${__('Refresh')}</button>
		</div>
		<div class="kubeport-site-rollout-result"></div>
	`);
	$body.find('.kubeport-site-rollout-refresh').on('click', () => kubeport_site_fetch_resource_rollout(frm));
	const $target = $body.find('.kubeport-site-rollout-result');
	const token = kubeport_site_create_observability_request_token(frm);
	$target.html(`<div class="text-muted small">${__('Loading rollout context…')}</div>`);
	frappe.call({
		method: 'kubeport.api.observability.get_site_resource_rollout',
		args: {
			site_docname: frm.doc.name,
			kind: state.row.kind,
			name: state.row.name,
			namespace: state.row.namespace || null,
			limit: 10
		}
	}).then((r) => {
		if (!kubeport_site_observability_request_is_active(frm, token)) return;
		if (r.exc) {
			$target.html(kubeport_site_render_panel_error(__('Could not fetch rollout context.')));
			return;
		}
		const payload = kubeport_site_normalize_observability_rows(r.message);
		$target.html(kubeport_site_render_rollout(payload.rows, payload.error));
	}, () => {
		if (!kubeport_site_observability_request_is_active(frm, token)) return;
		$target.html(kubeport_site_render_panel_error(__('Could not fetch rollout context.')));
	});
}

function kubeport_site_render_rollout(rows, error) {
	if (!rows.length) {
		return kubeport_site_render_panel_error(error)
			|| `<div class="text-muted small">${__('No rollout context was returned for this resource.')}</div>`;
	}
	let html = kubeport_site_render_panel_error(error);
	html += '<div class="table-responsive"><table class="table table-bordered" style="margin-bottom: 0;">';
	html += `<thead><tr>
		<th>${__('Revision')}</th><th>${__('State')}</th><th>${__('Created')}</th>
		<th>${__('Change Cause')}</th><th>${__('Images')}</th>
	</tr></thead><tbody>`;
	rows.forEach((row) => {
		const revision_state = row.current ? __('Current') : (row.update ? __('Update') : '');
		html += `<tr>
			<td>${frappe.utils.escape_html(String(row.revision || ''))}</td>
			<td>${frappe.utils.escape_html(revision_state)}</td>
			<td>${frappe.utils.escape_html(row.created_at || '')}</td>
			<td style="word-break: break-word;">${frappe.utils.escape_html(row.change_cause || '')}</td>
			<td style="word-break: break-all;">${frappe.utils.escape_html(row.image_summary || '')}</td>
		</tr>`;
	});
	html += '</tbody></table></div>';
	return html;
}

function kubeport_site_get_observability_body(frm) {
	const detail_field = frm.fields_dict.site_health_detail;
	if (!detail_field || !detail_field.$wrapper) return null;
	const $body = detail_field.$wrapper.find('.kubeport-site-observability-body');
	return $body.length ? $body : null;
}

function kubeport_site_normalize_observability_rows(message) {
	if (Array.isArray(message)) {
		return { rows: message, error: '' };
	}
	message = message || {};
	return {
		rows: message.rows || [],
		error: message.error || ''
	};
}

function kubeport_site_render_panel_error(error) {
	if (!error) return '';
	return `<div class="text-danger small" style="margin-bottom: 8px;">
		${frappe.utils.escape_html(error)}
	</div>`;
}

function kubeport_site_sort_rows(rows, key, direction) {
	const sorted = [...rows];
	sorted.sort((a, b) => {
		const left = a[key] || '';
		const right = b[key] || '';
		if (key === 'count') {
			return cint(left) - cint(right);
		}
		return String(left).localeCompare(String(right));
	});
	if (direction === 'desc') sorted.reverse();
	return sorted;
}

function kubeport_site_format_age(timestamp) {
	if (!timestamp) return '';
	if (typeof moment === 'function') {
		const parsed = moment(timestamp);
		if (parsed.isValid()) return parsed.fromNow();
	}
	return timestamp;
}
