#!/bin/bash
# Double-click this file in Finder to start the parcel explorer.
# A Terminal window will open, the server will start, and your browser
# will open to the app. Close the Terminal window to stop the server.

cd "$(dirname "$0")"
echo "Starting Propagentic Parcel Explorer..."
echo "Your browser should open in a few seconds."
echo "When you're done, close this window."
echo
python3 serve.py
