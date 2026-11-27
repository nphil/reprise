/* Decorative artwork for Library season headings. Only a heading on screen asks Plex for its poster. */
(() => {
    'use strict';
    const posterPath = /^\/api\/reprise\/media\/[a-zA-Z0-9_-]{1,128}\/poster$/;
    let observer;

    function seasonPoster(url) {
        if (typeof url !== 'string' || !posterPath.test(url)) return null;
        const image = document.createElement('img');
        image.className = 'rp-season-poster';
        image.alt = '';
        image.loading = 'lazy';
        image.decoding = 'async';
        image.dataset.posterUrl = url;
        image.addEventListener('error', () => image.remove(), { once: true });
        return image;
    }

    function load(image) {
        if (!image.isConnected || !image.dataset.posterUrl) return;
        image.src = image.dataset.posterUrl;
        delete image.dataset.posterUrl;
    }

    function watchSeasonPosters(list) {
        observer?.disconnect();
        const posters = list.querySelectorAll('img[data-poster-url]');
        if (!posters.length) return;
        if (!('IntersectionObserver' in window)) {
            posters.forEach(load); // The browser's own lazy loading still applies.
            return;
        }
        observer ??= new IntersectionObserver(entries => {
            for (const { isIntersecting, target } of entries) {
                if (!isIntersecting) continue;
                observer.unobserve(target);
                load(target);
            }
        });
        posters.forEach(poster => observer.observe(poster));
    }

    window.RepriseArtwork = { seasonPoster, watchSeasonPosters };
})();
