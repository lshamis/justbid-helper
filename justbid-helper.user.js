// ==UserScript==
// @name         JustBid 'Appears New' Helper
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  Automatically filters JustBid.com listings to only show items with the 'Appears New' condition.
// @author       You
// @match        *://*.justbid.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function() {
  'use strict';

  // Mock the browser.storage.local API using localStorage
  const browser = {
    storage: {
      local: {
        get: async function(keys) {
          if (keys === null) {
            const result = {};
            for (let i = 0; i < localStorage.length; i++) {
              const key = localStorage.key(i);
              if (key.startsWith("jb_")) {
                try {
                  result[key.slice(3)] = JSON.parse(localStorage.getItem(key));
                } catch (e) {}
              }
            }
            return result;
          }
          if (typeof keys === "string") {
            const val = localStorage.getItem("jb_" + keys);
            return val ? { [keys]: JSON.parse(val) } : {};
          }
          if (Array.isArray(keys)) {
            const result = {};
            for (const k of keys) {
              const val = localStorage.getItem("jb_" + k);
              if (val) result[k] = JSON.parse(val);
            }
            return result;
          }
          return {};
        },
        set: async function(obj) {
          for (const [k, v] of Object.entries(obj)) {
            localStorage.setItem("jb_" + k, JSON.stringify(v));
          }
        },
        remove: async function(keys) {
          if (typeof keys === "string") {
            localStorage.removeItem("jb_" + keys);
          } else if (Array.isArray(keys)) {
            for (const k of keys) {
              localStorage.removeItem("jb_" + k);
            }
          }
        }
      }
    }
  };

  /**
   * JustBid Condition Helper
   * Filters out items that are not "Appears New"
   */

  const CACHE_LIFETIME = 14 * 24 * 60 * 60 * 1000; // 14 days in ms

  async function getCachedCondition(url) {
    try {
      const data = await browser.storage.local.get(url);
      if (data[url]) {
        const { condition, timestamp } = data[url];
        if (Date.now() - timestamp < CACHE_LIFETIME) {
          return condition;
        } else {
          // Expired
          await browser.storage.local.remove(url);
        }
      }
    } catch (e) {
      console.error("JustBid Helper: Error reading from storage", e);
    }
    return null;
  }

  async function setCachedCondition(url, condition) {
    try {
      await browser.storage.local.set({
        [url]: {
          condition,
          timestamp: Date.now()
        }
      });
    } catch (e) {
      console.error("JustBid Helper: Error writing to storage", e);
    }
  }

  async function cleanupCache() {
    try {
      const allData = await browser.storage.local.get(null);
      const now = Date.now();
      const toRemove = [];
      
      for (const [url, entry] of Object.entries(allData)) {
        if (entry.timestamp && now - entry.timestamp > CACHE_LIFETIME) {
          toRemove.push(url);
        }
      }
      
      if (toRemove.length > 0) {
        await browser.storage.local.remove(toRemove);
        console.log(`JustBid Helper: Cleaned up ${toRemove.length} expired cache entries.`);
      }
    } catch (e) {
      console.error("JustBid Helper: Error during cache cleanup", e);
    }
  }

  async function fetchItemCondition(url) {
    const cached = await getCachedCondition(url);
    if (cached) return cached;
    
    try {
      const response = await fetch(url);
      const html = await response.text();
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, "text/html");
      
      const conditions = ["Appears New", "Open Box", "Pre-Owned", "Damaged", "Used", "As-Is"];
      const pageText = doc.body.innerText;
      
      const found = conditions.find(c => {
          const regex = new RegExp(`Condition:\\s*${c}|${c}`, 'i');
          return regex.test(pageText);
      });
      
      const result = found || "Unknown";
      await setCachedCondition(url, result);
      return result;
    } catch (e) {
      console.error(`JustBid Helper: Error fetching ${url}`, e);
      return "Error";
    }
  }

  async function processItem(link, preFetchedCondition = null) {
    // Mark as processed immediately to prevent duplicate runs
    link.setAttribute('data-justbid-processed', link.href);

    // Find the card container for this link
    const card = link.closest('div[class*="border"], div[class*="rounded"], div.grid > div') || link.parentElement;
    if (!card) return;
    
    const url = link.href;
    
    let condition;
    if (preFetchedCondition) {
      condition = preFetchedCondition;
    } else {
      // Set a "loading" state if we don't know the condition yet
      card.style.border = "2px dashed #94a3b8"; 
      condition = await fetchItemCondition(url);
    }
    
    if (condition !== "Unknown" && condition !== "Error") {
      console.log(`JustBid Helper: Item ${url.split('/').pop()} is "${condition}"`);
    }

    if (condition === "Appears New") {
      card.style.setProperty('border', '10px solid #22c55e', 'important');
      card.style.setProperty('opacity', '1', 'important');
      card.style.setProperty('display', '', 'important');
      // Remove any existing overlay if it was previously another condition
      const oldOverlay = card.querySelector('.justbid-condition-overlay');
      if (oldOverlay) oldOverlay.remove();
    } else if (condition !== "Unknown" && condition !== "Error") {
      card.style.setProperty('border', '10px solid #ef4444', 'important');
      card.style.setProperty('opacity', '0.3', 'important');
      card.style.setProperty('position', 'relative', 'important');

      // Add text overlay
      let overlay = card.querySelector('.justbid-condition-overlay');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.className = 'justbid-condition-overlay';
        overlay.style.cssText = `
          position: absolute;
          top: 50%;
          left: 50%;
          transform: translate(-50%, -50%);
          background: rgba(239, 68, 68, 0.9);
          color: white;
          padding: 8px 16px;
          border-radius: 8px;
          font-weight: bold;
          font-size: 1.2rem;
          z-index: 1000;
          pointer-events: none;
          text-transform: uppercase;
          box-shadow: 0 4px 6px rgba(0,0,0,0.1);
          white-space: nowrap;
        `;
        card.appendChild(overlay);
      }
      overlay.innerText = condition;
    }
    
    // Force the browser to paint the changes immediately
    requestAnimationFrame(() => {
       card.offsetHeight; // Reading a layout property forces a reflow
    });
  }

  async function filterJustBidItems() {
    console.log("JustBid Helper: Scanning for item links...");
    
    // Only select links that haven't been processed yet for their current URL
    const itemLinks = Array.from(document.querySelectorAll('a[href*="/item/"], a[href*="/products/"]'))
      .filter(link => link.getAttribute('data-justbid-processed') !== link.href);
      
    const uniqueLinks = [];
    const seenUrls = new Set();
    for (const link of itemLinks) {
      if (!seenUrls.has(link.href)) {
        seenUrls.add(link.href);
        uniqueLinks.push(link);
      }
    }

    console.log(`JustBid Helper: Found ${uniqueLinks.length} unique item links.`);

    // Load all cache at once to avoid multiple async calls
    const cache = await browser.storage.local.get(null);
    const now = Date.now();
    
    let networkRequestIndex = 0;

    for (const link of uniqueLinks) {
      const url = link.href;
      const cachedEntry = cache[url];
      
      if (cachedEntry && (now - cachedEntry.timestamp < CACHE_LIFETIME)) {
        // Highlight immediately if cached
        processItem(link, cachedEntry.condition);
      } else {
        // Stagger the network request
        setTimeout(() => {
          processItem(link);
        }, networkRequestIndex * 30);
        networkRequestIndex++;
      }
    }
  }

  // Function to run the filter with a debounce to handle rapid changes
  let filterTimeout;
  function triggerFilter() {
    clearTimeout(filterTimeout);
    filterTimeout = setTimeout(() => {
      filterJustBidItems();
    }, 300);
  }

  // Robust URL change detection for SPAs
  let currentUrl = location.href;

  // 1. Polling fallback (very reliable for SPAs)
  setInterval(() => {
    if (location.href !== currentUrl) {
      console.log("JustBid Helper: URL change detected via polling.");
      currentUrl = location.href;
      triggerFilter();
    }
  }, 500);

  // 2. Intercept History API (pushState and replaceState)
  const originalPushState = history.pushState;
  history.pushState = function() {
    originalPushState.apply(this, arguments);
    window.dispatchEvent(new Event('locationchange'));
  };

  const originalReplaceState = history.replaceState;
  history.replaceState = function() {
    originalReplaceState.apply(this, arguments);
    window.dispatchEvent(new Event('locationchange'));
  };

  window.addEventListener('locationchange', () => {
    console.log("JustBid Helper: URL change detected via History API.");
    currentUrl = location.href;
    triggerFilter();
  });

  // 3. Keep the MutationObserver for added nodes
  const observer = new MutationObserver((mutations) => {
    // Trigger if any nodes are added (such as infinite scroll elements on mobile)
    const hasAddedNodes = mutations.some(m => m.addedNodes && m.addedNodes.length > 0);
    if (hasAddedNodes) {
      triggerFilter();
    }
  });

  observer.observe(document.body, {
    childList: true,
    subtree: true
  });

  // Run initially
  cleanupCache();
  filterJustBidItems();

  console.log("JustBid 'Appears New' Helper active.");
})();
