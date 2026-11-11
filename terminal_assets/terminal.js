(() => {
  "use strict";
  const terminal = new Terminal({
    cursorBlink: true,
    fontFamily: "Menlo, Monaco, monospace",
    fontSize: 14,
    scrollback: 5000,
    theme: { background: "#171c23", foreground: "#e7edf5", cursor: "#e7edf5" }
  });
  const fit = new FitAddon.FitAddon();
  terminal.loadAddon(fit);
  terminal.open(document.getElementById("terminal"));
  let bridge = null;
  let enabled = false;
  terminal.onData(data => {
    if (enabled && bridge) bridge.sendInput(data);
  });
  terminal.onResize(({ cols, rows }) => {
    if (bridge) bridge.resizeTerminal(cols, rows);
  });
  new ResizeObserver(() => fit.fit()).observe(document.getElementById("terminal"));
  new QWebChannel(qt.webChannelTransport, channel => {
    bridge = channel.objects.terminalBridge;
    bridge.output.connect(chunk => terminal.write(chunk));
    bridge.clear.connect(() => terminal.reset());
    bridge.enabled.connect(value => {
      enabled = value;
      if (value) terminal.focus();
    });
    fit.fit();
    bridge.resizeTerminal(terminal.cols, terminal.rows);
    bridge.ready();
  });
})();
