#!/bin/bash
# Test script to verify Google Sheets integration

echo "🔍 Checking Google Sheets Integration..."
echo ""

# Check if credentials exist
if [ ! -f ~/.google_credentials.json ]; then
    echo "❌ Missing: ~/.google_credentials.json"
    echo "   Download from: https://console.cloud.google.com/"
    echo "   Enable: Google Sheets API + YouTube Data API v3"
else
    echo "✓ Credentials found"
fi

echo ""

# Check .env file
if [ ! -f ~/.env ]; then
    echo "❌ Missing: ~/.env"
    echo "   Run: cp ~/.env.example ~/.env"
else
    echo "✓ .env exists"
    
    # Check if Sheet IDs are set
    if grep -q "SPRINT_SHEET_ID=" ~/.env; then
        SPRINT=$(grep "SPRINT_SHEET_ID=" ~/.env | cut -d= -f2)
        if [ -z "$SPRINT" ]; then
            echo "   ⚠ SPRINT_SHEET_ID is empty"
        else
            echo "   ✓ SPRINT_SHEET_ID configured"
        fi
    fi
    
    if grep -q "BACKLOG_SHEET_ID=" ~/.env; then
        BACKLOG=$(grep "BACKLOG_SHEET_ID=" ~/.env | cut -d= -f2)
        if [ -z "$BACKLOG" ]; then
            echo "   ⚠ BACKLOG_SHEET_ID is empty"
        else
            echo "   ✓ BACKLOG_SHEET_ID configured"
        fi
    fi
fi

echo ""
echo "📋 Sheet URLs:"
echo "   Sprint:  https://docs.google.com/spreadsheets/d/1qegkgolGQDbkAB7zCtxEG-PgCJ8lapC9-kJZUoeirCA"
echo "   Backlog: https://docs.google.com/spreadsheets/d/1q478v3TCARlagB_6tR4g1rz9HpHBJU6WYftyBbIOodQ"

echo ""
echo "🧪 Test configuration:"
echo "   1. Check that sheets have data"
echo "   2. Sprint sheet: Column B has dates (21.08.2026, 25.08.2026)"
echo "   3. Backlog sheet: Column C has status (ready, готово, in progress)"
echo "   4. Run: python3 ~/cyber_panel.py"

echo ""
echo "💡 Tips:"
echo "   - Sprint sheet shows only next 15 days"
echo "   - Backlog sheet hides 'готово' tasks"
echo "   - First run will ask for OAuth authorization"
echo ""
