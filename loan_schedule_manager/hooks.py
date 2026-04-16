app_name = "loan_schedule_manager"
app_title = "Loan Schedule Manager"
app_publisher = "Upande"
app_description = "Manages bank loan repayment schedules and auto-creates journal entries"
app_version = "1.0.0"

# DocType Events
doc_events = {
    "Journal Entry": {
        "on_submit": "loan_schedule_manager.events.journal_entry.on_submit",
        "on_cancel": "loan_schedule_manager.events.journal_entry.on_cancel",
        "on_update_after_submit": "loan_schedule_manager.events.journal_entry.on_update_after_submit",
    }
}

# Scheduler Events
scheduler_events = {
    "daily": [
        "loan_schedule_manager.scheduler.tasks.create_due_loan_journal_entries",
        "loan_schedule_manager.scheduler.notifications.send_overdue_alerts",
    ]
}

# Fixtures - export these DocTypes when running bench export-fixtures
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [["module", "=", "Loan Schedule Manager"]]
    },
    {
        "dt": "Web Page",
        "filters": [["module", "=", "Loan Schedule Manager"]]
    }
]