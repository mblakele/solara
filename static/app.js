/* solara dashboard client.
 *
 * Two cooperating update paths:
 *
 *  1. Fallback — the server emits a <meta http-equiv="refresh"> plus a
 *     sleep-hint; the JS schedules a visibility-aware full reload. This is
 *     what keeps the page fresh when SSE is unavailable (no JS, old proxy,
 *     load management disabled).
 *
 *  2. Live — EventSource('/stream/status'). Each metrics/load event triggers
 *     a small fragment fetch (`?partial=metrics`, `?partial=load`) and swaps
 *     the section's innerHTML in place. All markup stays server-rendered
 *     (single source of truth in the Jinja templates). Only a fragment that
 *     marks itself SSE-driven (`data-live="1"` — load management enabled)
 *     removes the meta refresh and reload timer; without a driver the page
 *     keeps bouncing on its own cadence.
 *
 *  The metrics fragment also carries a data-freshness strip (`#data-freshness`):
 *  how old the per-second data is, when to expect the next update, and — once
 *  SSE proves live — the `#live-indicator` badge nested inside it. The server
 *  renders age and offsets once; this client ticks them forward per second.
 */
(function () {
  'use strict'

  var defaultMax = 2 * 60 * 1000 // 2 minutes — fallback
  var sleepHintEl = document.getElementById('sleep-hint')
  var sleepHintAtEl = document.getElementById('sleep-hint-at')
  var millisMax = sleepHintEl
    ? Math.max(15 * 1000, (parseFloat(sleepHintEl.dataset.value) || 0) * 1000)
    : defaultMax

  // Validate freshness: if the hint was computed long ago, it may be stale.
  if (sleepHintAtEl) {
    var hintAt = new Date(sleepHintAtEl.dataset.value).getTime()
    var hintAge = Date.now() - hintAt
    if (hintAge > millisMax) {
      console.log('sleep hint stale (age=%dms), using 15s fallback', hintAge)
      millisMax = 15 * 1000
    }
  }

  var reloadTimer = null
  var live = false

  function timestamp() {
    return new Date(Date.now()).toLocaleTimeString()
  }

  function log(label, extra) {
    if (window.console && console.log) {
      console.log(label, extra || '', timestamp())
    }
  }

  function reloadIfVisible() {
    if (document.visibilityState === 'hidden') {
      // Reload later — the timer is throttled in background tabs.
      reloadTimer = setTimeout(reloadIfVisible, 1000)
      return
    }
    log('reloading dashboard')
    window.location.reload()
  }

  function scheduleReload() {
    reloadTimer = setTimeout(reloadIfVisible, millisMax)
  }

  // Data-freshness strip (templates/_metrics.html): the server renders the
  // age, next-update offsets, and the mode-aware warn/stale thresholds
  // (constants.py FRESHNESS_{RELOAD,LIVE}_*_SECS); this ticks age forward
  // every second and shifts data-status fresh -> aging -> stale.
  function tickFreshness() {
    var el = document.getElementById('data-freshness')
    if (!el) {
      return
    }
    var lagAtMs = parseInt(el.getAttribute('data-lag-at'), 10)
    if (!isFinite(lagAtMs)) {
      return
    }
    var lag = parseFloat(el.getAttribute('data-lag')) || 0
    var next = parseFloat(el.getAttribute('data-next')) || 0
    var warn = parseFloat(el.getAttribute('data-warn'))
    var stale = parseFloat(el.getAttribute('data-stale'))
    if (!isFinite(warn)) {
      warn = 300
    }
    if (!isFinite(stale)) {
      stale = 420
    }
    var elapsed = Math.max(0, (Date.now() - lagAtMs) / 1000)
    var age = lag + elapsed
    var ageEl = el.querySelector('.freshness__age')
    if (ageEl) {
      ageEl.textContent = Math.round(age) + 's'
    }
    var status = age >= stale ? 'stale' : age >= warn ? 'aging' : 'fresh'
    if (el.getAttribute('data-status') !== status) {
      el.setAttribute('data-status', status)
    }
    var remain = next - elapsed
    var nextEl = el.querySelector('.freshness__next')
    if (nextEl) {
      nextEl.textContent = (remain >= 0 ? Math.ceil(remain) : 0) + 's'
    }
  }

  setInterval(tickFreshness, 1000)

  function cancelAutoRefresh() {
    var meta = document.querySelector('meta[http-equiv="refresh"]')
    if (meta && meta.parentNode) {
      meta.parentNode.removeChild(meta)
    }
    if (reloadTimer !== null) {
      window.clearTimeout(reloadTimer)
      reloadTimer = null
    }
  }

  document.addEventListener('visibilitychange', function () {
    log('visibilitychange', document.visibilityState)
  })

  window.addEventListener('focus', function () {
    var millis = Date.now() - window.__solaraTimePrev
    log('focus after', millis + 'ms')
    if (!live && millis > millisMax) {
      log('reloading via focus')
      window.location.reload()
    }
  })
  window.__solaraTimePrev = Date.now()

  // The live badge lives inside the metrics fragment, which is swapped out on
  // every SSE update — so always re-query it and re-apply the connected state.
  function applyLiveBadge() {
    var el = document.getElementById('live-indicator')
    if (el) {
      el.removeAttribute('hidden')
      el.setAttribute('data-state', 'connected')
    }
  }

  function markLive() {
    if (live) {
      return
    }
    live = true
    cancelAutoRefresh()
    applyLiveBadge()
    log('live updates active via /stream/status')
  }

  function swapSection(id, url, selector) {
    fetch(url, { headers: { Accept: 'text/html' } })
      .then(function (resp) {
        return resp.ok ? resp.text() : ''
      })
      .then(function (html) {
        var node = document.getElementById(id)
        if (!node) {
          return
        }
        node.innerHTML = html
        // The fresh fragment re-hides the live badge (it is server-rendered
        // hidden), so re-apply the connected state when we're already live.
        if (live) {
          applyLiveBadge()
        }
        // Only switch to live updates once a swap proves the page is
        // SSE-driven ([data-live="1"]): load management is enabled and the
        // background loop keeps pushing events. With load management off,
        // SSE is a one-shot snapshot — keep the server auto-refresh / reload
        // timer alive so the page keeps self-healing.
        if (!live && node.querySelector('[data-live="1"]') && node.querySelector(selector)) {
          markLive()
        }
      })
      .catch(function (err) {
        // Keep the auto-refresh fallback in place.
        log('partial fetch failed', err)
      })
  }

  if (window.EventSource) {
    var source = new EventSource('/stream/status')
    source.addEventListener('initial_metrics', function () {
      swapSection('metrics-section', '?partial=metrics', '.forecast')
    })
    source.addEventListener('metrics_update', function () {
      swapSection('metrics-section', '?partial=metrics', '.forecast')
    })
    source.addEventListener('initial_load_state', function () {
      swapSection('load-management-section', '?partial=load', '.load-management')
    })
    source.addEventListener('load_cycle', function () {
      swapSection('load-management-section', '?partial=load', '.load-management')
    })
    // EventSource reconnects automatically; no explicit retry needed.
    // If the connection dies permanently the fallback refresh still works
    // unless we already switched to live (in which case a reconnect will
    // resume updates).
  }

  scheduleReload()
})()