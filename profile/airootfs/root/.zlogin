# fix for screen readers
if grep -Fqa 'accessibility=' /proc/cmdline &> /dev/null; then
    setopt SINGLE_LINE_ZLE
fi

[[ -x ~/.automated_script.sh ]] && ~/.automated_script.sh

# Start the installer on the first virtual console only.
if [[ "$(tty)" == "/dev/tty1" ]]; then
    archinstall-launcher
    print "\nThe installer has exited. Run 'archinstall-launcher' to start it again."
fi
