# chhaba-bots

Scheduled jobs for the Case Desk of the Office of Sh. B.S. Chhaba, AAG.

- **HC causelist** (`.github/workflows/hc-causelist.yml`): reads the Rajasthan High Court's public
  "Quick Download" causelists of Jaipur and Jodhpur every morning (and in the evening for the next day)
  and publishes them to the `hc-data` branch, where the Case Desk reads them.
- **Daily reports and watchdog** (`.github/workflows/daily-report.yml`): the morning and evening reports
  (posted inside the Case Desk, never printed here) and an uptime check of the website.

The website and app code are kept in a separate private repository.
