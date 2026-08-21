#!/bin/bash
# Startup script for Cyber Panel with Extended HUD

echo "🔧 Starting Cyber Panel..."
echo ""
echo "Requirements:"
echo "  ✓ Second monitor must be connected"
echo "  ✓ Display environment variable set (if needed)"
echo ""

# Check if running in WSL on Windows with second monitor
if command -v powershell.exe &> /dev/null; then
    echo "📺 Detecting monitors..."
    monitors=$(powershell.exe "Get-CimInstance -ClassName Win32_VideoController | Measure-Object | Select-Object -ExpandProperty Count")
    if [ "$monitors" -gt 1 ] 2>/dev/null || [ "$monitors" == "2" ] 2>/dev/null; then
        echo "✓ Multiple displays detected"
    else
        echo "⚠ Warning: Only one display detected, Extended HUD may not appear"
    fi
    echo ""
fi

# Check environment file
if [ ! -f ~/.env ]; then
    echo "⚠ Warning: ~/.env not found"
    echo "  Copy from ~/.env.example and fill in your API keys:"
    echo "    cp ~/.env.example ~/.env"
    echo ""
fi

echo "🚀 Launching application..."
python3 ~/cyber_panel.py
