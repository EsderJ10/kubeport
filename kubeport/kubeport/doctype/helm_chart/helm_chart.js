frappe.ui.form.on('Helm Chart', {
    refresh: function(frm) {
        if (!frm.is_new()) {
            // Add "Install" button that creates a new Helm Release pre-filled with this chart
            frm.add_custom_button(__('Deploy as Release'), function() {
                frappe.new_doc('Helm Release', {
                    chart: frm.doc.name,
                    chart_version: frm.doc.latest_version,
                });
            }, __('Actions'));

            // Add "Fetch Values" button
            frm.add_custom_button(__('Fetch Default Values'), function() {
                frappe.call({
                    doc: frm.doc,
                    method: 'fetch_default_values',
                    freeze: true,
                    freeze_message: __('Fetching default values from Helm...'),
                    callback: function(r) {
                        if (!r.exc) {
                            frm.reload_doc();
                            frappe.show_alert({
                                message: __('Default values updated.'),
                                indicator: 'green'
                            });
                        }
                    }
                });
            }, __('Actions'));

            // Show version count
            if (frm.doc.versions && frm.doc.versions.length) {
                frm.dashboard.add_indicator(
                    __('{0} Versions Available', [frm.doc.versions.length]),
                    'blue'
                );
            }
        }
    }
});
