#!/bin/bash

# Configuration
REPO_URL="https://github.com/hyper440/aebn-vod-downloader"
API_URL="https://api.github.com/repos/hyper440/aebn-vod-downloader/commits/main"
MANIFEST_FILE="manifest.json"
Backup_DIR="backups"
SOURCE_DIR="source"

# Ensure directories exist
mkdir -p "$SOURCE_DIR"
mkdir -p "$Backup_DIR"

# Function to get latest SHA
get_latest_sha() {
    curl -s "$API_URL" | grep '"sha":' | head -n 1 | cut -d '"' -f 4
}

# Function to read manual SHA
read_manifest_sha() {
    if [ -f "$MANIFEST_FILE" ]; then
        grep '"last_known_good_sha":' "$MANIFEST_FILE" | cut -d '"' -f 4
    else
        echo ""
    fi
}

# Function to update manifest
update_manifest() {
    local sha=$1
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "{\"last_known_good_sha\": \"$sha\", \"last_update_timestamp\": \"$timestamp\"}" > "$MANIFEST_FILE"
}

echo "Checking for updates..."

LATEST_SHA=$(get_latest_sha)
CURRENT_SHA=$(read_manifest_sha)

if [ -z "$LATEST_SHA" ]; then
    echo "Warning: Could not fetch latest SHA (Network issue or Rate Limit)."
    echo "Attempting to revert to last successfully pulled local backup..."
    
    # Check if we have a valid source
    if [ -d "$SOURCE_DIR/.git" ]; then
        echo "Using existing local source."
        exit 0
    elif [ -n "$CURRENT_SHA" ] && [ -d "$Backup_DIR/$CURRENT_SHA" ]; then
         echo "Restoring from backup: $CURRENT_SHA"
         rm -rf "$SOURCE_DIR"
         cp -r "$Backup_DIR/$CURRENT_SHA" "$SOURCE_DIR"
         exit 0
    else
        echo "Error: No source available and no backup found."
        exit 1
    fi
fi

if [ "$LATEST_SHA" == "$CURRENT_SHA" ]; then
    echo "Source is up to date (SHA: $LATEST_SHA). Using cached version."
    exit 0
else
    echo "New version detected (Old: $CURRENT_SHA, New: $LATEST_SHA)."
    echo "Updating source..."
    
    # Clean source dir
    rm -rf "$SOURCE_DIR"
    
    # Clone new version
    git clone --depth 1 "$REPO_URL" "$SOURCE_DIR"
    
    if [ $? -eq 0 ]; then
        echo "Update successful."
        update_manifest "$LATEST_SHA"
        
        # Create backup
        echo "Creating backup..."
        cp -r "$SOURCE_DIR" "$Backup_DIR/$LATEST_SHA"
        
        # Cleanup old backups (keep last 5)
        # ls -dt "$Backup_DIR"/* | tail -n +6 | xargs rm -rf
    else
        echo "Error: Git clone failed."
        exit 1
    fi
fi
