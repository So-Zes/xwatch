// ホーム画面に追加できるようにするためだけのもの。投稿や API の返事はキャッシュしない
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
