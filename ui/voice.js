(() => {
  const $ = (id) => document.getElementById(id);
  let context = { node: { id: "local", name: "local" }, config: {} };
  let pressed = false;
  let starting = false;
  let recording = false;
  let busy = false;
  let attempt = 0;
  let startTimer = 0;
  let stopTimer = 0;

  const START_TIMEOUT_MS = 35000;
  const CLIP_TIMEOUT_MS = 15000;
  const ANSWER_TIMEOUT_MS = 90000;

  const withTimeout = (promise, ms, message) => {
    let timer = 0;
    const limit = new Promise((_, reject) => {
      timer = window.setTimeout(() => reject(new Error(message)), ms);
    });
    return Promise.race([promise, limit]).finally(() => window.clearTimeout(timer));
  };

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
    if (!busy) setLabel(active ? "Listening… release" : "Hold to talk");
  };

  async function start() {
    if (recording || starting || busy) return;
    starting = true;
    const id = ++attempt;
    setStatus("Requesting microphone…");
    const waitTimer = window.setTimeout(() => {
      if (id === attempt && starting && !recording) {
        setStatus("Waiting for microphone permission…");
      }
    }, 8000);
    startTimer = window.setTimeout(() => {
      if (id !== attempt) return;
      // Abandon this attempt so a press after the timeout is not blocked by
      // a permission prompt that never settled.
      attempt += 1;
      starting = false;
      pressed = false;
      window.clearTimeout(waitTimer);
      // Release the microphone if the request completed after the timeout.
      window.orqella.call("media.record.stop", {}).catch(() => {});
      setStatus("Microphone did not respond. Allow microphone access for Orqella, then hold again.", "bad");
    }, START_TIMEOUT_MS);
    try {
      await window.orqella.call("media.record.start", {});
      window.clearTimeout(waitTimer);
      if (id !== attempt) return;
      if (!pressed) {
        try { await window.orqella.call("media.record.stop", {}); } catch {}
        setStatus("Hold the button, speak, then release.");
        return;
      }
      window.clearTimeout(startTimer);
      startTimer = 0;
      setRecording(true);
      setStatus("Listening… release to send.");
    } catch (error) {
      window.clearTimeout(waitTimer);
      if (id === attempt) {
        pressed = false;
        setStatus(`Microphone error: ${error?.message || error}`, "bad");
      }
    } finally {
      if (id === attempt) {
        window.clearTimeout(startTimer);
        startTimer = 0;
        starting = false;
      }
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
    const id = ++attempt;
    stopTimer = window.setTimeout(() => {
      if (id !== attempt) return;
      attempt += 1;
      busy = false;
      if (button) button.disabled = false;
      setLabel("Hold to talk");
      setStatus("The voice assistant did not answer in time. Try again.", "bad");
    }, ANSWER_TIMEOUT_MS);
    try {
      const clip = await withTimeout(
        window.orqella.call("media.record.stop", {}),
        CLIP_TIMEOUT_MS,
        "The microphone did not return a recording.",
      );
      if (id !== attempt) return;
      if (!clip?.size || clip.size < 600) {
        setStatus("That was too short.", "bad");
        return;
      }
      const result = await window.orqella.call("backend.request", {
        method: "POST",
        path: "/api/voice",
        body: { audio: clip.audio, mime: clip.mime },
      });
      if (id !== attempt) return;
      render(result);
    } catch (error) {
      if (id === attempt) setStatus(error?.message || "Voice assistant failed.", "bad");
    } finally {
      if (id === attempt) {
        window.clearTimeout(stopTimer);
        stopTimer = 0;
        busy = false;
        if (button) button.disabled = false;
        setLabel("Hold to talk");
      }
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
    let touchActive = false;

    const begin = () => {
      if (busy || starting || recording) return;
      pressed = true;
      void start();
    };
    const end = () => {
      if (!pressed && !recording) return;
      void stop();
    };

    // Touch events are handled separately from pointer events. On iOS Safari a
    // long press otherwise starts the native text selection/loupe and can
    // cancel the pointer sequence before the release is delivered. A
    // non-passive touchstart listener that calls preventDefault suppresses
    // that gesture without blocking the microphone request.
    button.addEventListener("touchstart", (event) => {
      if (event.touches.length > 1) return;
      event.preventDefault();
      touchActive = true;
      begin();
    }, { passive: false });
    button.addEventListener("touchend", (event) => {
      event.preventDefault();
      if (!touchActive) return;
      touchActive = false;
      end();
    }, { passive: false });
    button.addEventListener("touchcancel", (event) => {
      event.preventDefault();
      if (!touchActive) return;
      touchActive = false;
      end();
    }, { passive: false });

    button.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "touch") return;
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      try { button.setPointerCapture?.(event.pointerId); } catch {}
      begin();
    });
    button.addEventListener("pointerup", (event) => {
      if (event.pointerType === "touch") return;
      event.preventDefault();
      end();
    });
    button.addEventListener("pointercancel", (event) => {
      if (event.pointerType === "touch") return;
      end();
    });
    button.addEventListener("lostpointercapture", (event) => {
      if (event.pointerType === "touch") return;
      end();
    });

    for (const type of ["contextmenu", "dragstart", "selectstart"]) {
      button.addEventListener(type, (event) => event.preventDefault());
    }
    button.addEventListener("keydown", (event) => {
      if ((event.key === " " || event.key === "Enter") && !event.repeat) {
        event.preventDefault();
        begin();
      }
    });
    button.addEventListener("keyup", (event) => {
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        end();
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) return;
      pressed = false;
      void stop();
    });

    window.orqella.ready(async (ctx) => {
      context = ctx;
      try {
        const status = await window.orqella.call("backend.request", { method: "GET", path: "/api/status" });
        if (pressed || starting || recording || busy) return;
        if (status.ready) setStatus("Ready.");
        else if (status.job === "running") setStatus("Setting up voice engine…");
        else setStatus("Voice engine setup required.", "bad");
      } catch (error) {
        if (pressed || starting || recording || busy) return;
        setStatus(error.message || "Voice assistant unavailable.", "bad");
      }
    });
  }

  wire();
})();
