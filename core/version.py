# Single source of truth for the app's displayed version AND the service
# worker's cache-busting string (see templates/sw.js). Bump this on every
# deploy that changes any cached static asset — that's what makes the
# Download page's "Update" button actually pull fresh files instead of
# reporting "up to date" against a cache that never changed.
APP_VERSION = "1.36.7"
