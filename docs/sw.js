/**
 * McFetridge Tennis — Service Worker
 * Background slot checking + push notifications for new availability.
 */

const CACHE_NAME = 'tennis-v1';
const DATA_URL = 'data/status.json';

self.addEventListener('install', function(event) {
    self.skipWaiting();
});

self.addEventListener('activate', function(event) {
    event.waitUntil(clients.claim());
});

// Periodic background sync (Chrome 80+, requires user engagement)
self.addEventListener('periodicsync', function(event) {
    if (event.tag === 'check-slots') {
        event.waitUntil(checkForNewSlots());
    }
});

async function checkForNewSlots() {
    try {
        var resp = await fetch(DATA_URL + '?_=' + Date.now());
        if (!resp.ok) return;
        var data = await resp.json();

        // Get previous data from cache
        var cache = await caches.open(CACHE_NAME);
        var cachedResp = await cache.match('last-slots');
        var oldData = cachedResp ? await cachedResp.json() : null;

        // Store current data
        await cache.put('last-slots', new Response(JSON.stringify(data)));

        if (!oldData) return;

        // Diff slots
        var oldSlots = new Set();
        (oldData.calendar || []).forEach(function(d) {
            (d.slots || []).forEach(function(s) {
                oldSlots.add(d.date + '|' + s.slot_time + '|' + (s.court_name || ''));
            });
        });

        var newSlots = [];
        (data.calendar || []).forEach(function(d) {
            (d.slots || []).forEach(function(s) {
                var key = d.date + '|' + s.slot_time + '|' + (s.court_name || '');
                if (!oldSlots.has(key)) {
                    newSlots.push({
                        date: d.date,
                        time: s.slot_time,
                        court: s.court_name || '',
                        dayName: d.day_name,
                    });
                }
            });
        });

        if (newSlots.length > 0) {
            var body = newSlots.map(function(s) {
                return s.dayName + ' ' + s.time + (s.court ? ' ' + s.court : '');
            }).join(', ');

            self.registration.showNotification('New Tennis Court Available!', {
                body: body,
                icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">&#127934;</text></svg>',
                tag: 'new-slots',
                renotify: true,
                data: { url: self.location.origin + self.location.pathname.replace('sw.js', '') },
            });
        }
    } catch (e) {
        // Silent fail for background sync
    }
}

// Open app when notification is clicked
self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    var url = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil(clients.openWindow(url));
});
