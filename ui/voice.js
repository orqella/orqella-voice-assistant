(() => {
  const $ = (id) => document.getElementById(id);
  let context = { node: { id: "local", name: "local" }, config: {} };
  let pressed = false;
  let starting = false;
  let recording = false;
  let busy = false;

  const setStatus = (text, kind = "") => {
    const el = $("voiceStatus");
    if (!el) return;
    el.textContent = text;
    el.className = `voice-status ${kind}`;
  };
  const setLabel = (text) => { const el = $("voiceLabel"); if (el) el.textContent = text; };
  const setRecording = (active) => {
    recording = active;
    const button = $("voiceButton");
    if (button) {
      button.classList.toggle("is-recording", active);
      button.setAttribute("aria-pressed", String(active));
    }
    setLabel(active ? "Listening… release" : "Hold to talk");
  };

  async function start() {
    if (recording || starting || busy) return;
    starting = true;
    setStatus("Requesting microphone…");
    try {
      await window.orqella.call("media.record.start", {});
      if (!pressed) {
        try { await window.orqella.call("media.record.stop", {}); } catch {}
        setStatus("Hold the button, speak, then release.");
        return;
      }
      setRecording(true);
      setStatus("Listening… release to send.");
    } catch (error) {
      setStatus(`Microphone error: ${error.message || error}`, "bad");
      pressed = false;
    } finally {
      starting = false;
    }
  }

  async function stop() {
    pressed = false;
    if (!recording) return;
    setRecording(false);
    setStatus("Thinking…");
    setLabel("Thinking…");
    const button = $("voiceButton");
    if (button) button.disabled = true;
    busy = true;
    try {
      const clip = await window.orqella.call("media.record.stop", {});
      if (!clip?.size || clip.size < 600) {
        setStatus("That was too short.", "bad");
        return;
      }
      const result = await window.orqella.call("backend.request", {
        method: "POST",
        path: "/api/voice",
        body: { audio: clip.audio, mime: clip.mime },
      });
      render(result);
    } catch (error) {
      setStatus(error.message || "Voice assistant failed.", "bad");
    } finally {
      busy = false;
      if (button) button.disabled = false;
      setLabel("Hold to talk");
    }
  }

  function render(data) {
    const transcript = String(data.transcript || "").trim();
    const reply = String(data.say || "").trim();
    const exchange = $("voiceExchange");
    if (exchange) exchange.hidden = !(transcript || reply);
    if ($("voiceTranscript")) $("voiceTranscript").textContent = transcript;
    if ($("voiceReply")) $("voiceReply").textContent = reply || "Done.";
    if (data.error) setStatus(String(data.error), "bad");
    else if (data.route?.name) setStatus(`${data.route.name} · ${reply || "Done."}`, "");
    else setStatus(reply || "Done.");
  }

  function wire() {
    const button = $("voiceButton");
    if (!button) return;
    const down = async (event) => {
      event.preventDefault();
      pressed = true;
      button.setPointerCapture?.(event.pointerId);
      await start();
    };
    const up = (event) => { event.preventDefault(); stop(); };
    button.addEventListener("pointerdown", down);
    button.addEventListener("pointerup", up);
    button.addEventListener("pointercancel", up);
    button.addEventListener("lostpointercapture", () => stop());
    button.addEventListener("contextmenu", (event) => event.preventDefault());
    button.addEventListener("keydown", (event) => {
      if ((event.key === " " || event.key === "Enter") && !event.repeat) {
        event.preventDefault();
        pressed = true;
        start();
      }
    });
    button.addEventListener("keyup", (event) => {
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        stop();
      }
    });
    window.addEventListener("blur", () => stop());
    window.orqella.ready(async (ctx) => {
      context = ctx;
      try {
        const status = await window.orqella.call("backend.request", { method: "GET", path: "/api/status" });
        if (status.ready) setStatus("Ready.");
        else if (status.job === "running") setStatus("Setting up voice engine…");
        else setStatus("Voice engine setup required.", "bad");
      } catch (error) {
        setStatus(error.message || "Voice assistant unavailable.", "bad");
      }
    });
  }

  wire();
})();
