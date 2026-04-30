// Copyright (c) 2026, Los Favs and contributors
// For license information, please see license.txt

frappe.ui.form.on("Kubernetes Command", {
	refresh(frm) {
		const status = frm.doc.status;
		const indicator = {
			Pending: "orange",
			Running: "blue",
			Completed: "green",
			Failed: "red",
		}[status];
		if (indicator) {
			frm.page.set_indicator(__(status), indicator);
		}

		if (status === "Pending" && !frm.is_new()) {
			frm.add_custom_button(__("Execute"), () => execute_command(frm), __("Actions"));
		}
	},
});

function execute_command(frm) {
	const action = frm.doc.action;
	if (action === "Delete" && !frm.doc.confirm_destructive) {
		frappe.msgprint({
			title: __("Confirmation Required"),
			message: __(
				"Check <b>Confirm Destructive</b> and save the form before executing a Delete."
			),
			indicator: "red",
		});
		return;
	}

	const proceed = () => {
		frm.call("execute")
			.then((r) => {
				if (!r.message) return;
				if (r.message.queued) {
					frappe.show_alert({
						message: __("Delete enqueued. Output will appear when the worker finishes."),
						indicator: "blue",
					});
				}
				frm.reload_doc();
			})
			.catch(() => {
				frm.reload_doc();
			});
	};

	if (action === "Delete") {
		frappe.confirm(
			__("Delete {0} <b>{1}</b> in namespace <b>{2}</b> on cluster <b>{3}</b>? This cannot be undone.", [
				frm.doc.resource_kind,
				frm.doc.resource_name,
				frm.doc.namespace,
				frm.doc.cluster,
			]),
			proceed
		);
	} else {
		proceed();
	}
}
