#!/bin/bash

# GPU Fan Control Installation Script
# Targets: Ubuntu (A6000 or 3090 machines)

set -e

# --- Configuration ---
PROJECT_DIR="$(pwd)"
VENV_PATH="$PROJECT_DIR/venv"
DISPLAY_VAL=":1"

echo "🚀 Starting GPU Fan Control Installation..."

# 1. Check for root/sudo privileges
if [ "$EUID" -ne 0 ]; then
  echo "⚠️  Warning: Some steps require sudo. You may be prompted for your password."
fi

# 2. Install System Dependencies
echo "📦 Installing system dependencies..."
apt-get update
apt-get install -y nvidia-settings xserver-xorg-core x11-xserver-utils python3-venv

# 3. Enable Coolbits 28 (Crucial for manual fan control)
echo "⚙️  Enabling Coolbits 28 in Xorg configuration..."
# This command adds the Coolbits option to the Device section of the X config
# It's the standard way to unlock fan control on NVIDIA Linux drivers
if grep -q "Coolbits" /etc/X11/xorg.conf 2>/dev/null; then
    echo "Coolbits already present in /etc/X11/xorg.conf"
else
    # If xorg.conf doesn't exist, we create a minimal one or add to xorg.conf.d
    # We use the nvidia-xconfig tool if available, otherwise we append to the config
    if command -v nvidia-xconfig >/dev/null 2>&1; then
        nvidia-xconfig --cool-bits=28
        echo "✅ Coolbits enabled via nvidia-xconfig"
    else
        echo "❌ nvidia-xconfig not found. Please run: sudo nvidia-xconfig --cool-bits=28"
        echo "   and restart your X server."
    fi
fi

# 4. Setup Python Virtual Environment
echo "🐍 Setting up Python environment..."
cd "$PROJECT_DIR"
if [ ! -d "$VENV_PATH" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Configure Systemd User Services
echo "🛠️  Configuring systemd user services..."
mkdir -p ~/.config/systemd/user/

# Copy the service files from the project directory to the user's systemd folder
cp "$PROJECT_DIR/systemd/gpu-fan-control.service" ~/.config/systemd/user/
cp "$PROJECT_DIR/systemd/gpu-fan-control-ui.service" ~/.config/systemd/user/

# Update the service files to use the correct environment and DISPLAY
# We use sed to ensure the DISPLAY variable is set correctly
sed -i "s/DISPLAY=:.*/DISPLAY=$DISPLAY_VAL/" ~/.config/systemd/user/gpu-fan-control.service

# 6. Finalizing and Verification
echo "🏁 Installation complete!"
echo "-------------------------------------------------------------------"
echo "NEXT STEPS:"
echo "1. REBOOT your machine (or restart X server) for Coolbits to take effect."
echo "2. Enable the daemon to start on boot:"
echo "   systemctl --user daemon-reload"
echo "   systemctl --user enable gpu-fan-control"
echo "   systemctl --user start gpu-fan-control"
echo ""
echo "3. Start the UI (Optional):"
echo "   systemctl --user start gpu-fan-control-ui"
echo "   Then open: http://localhost:8505"
echo "-------------------------------------------------------------------"
echo "Verification Command:"
echo "To check if manual control is working, run:"
echo "DISPLAY=$DISPLAY_VAL nvidia-settings -a '[gpu:0]/GPUFanControlState=1'"
