package main

import (
	"context"
	"net/url"
	"time"
)

// Share a modest request budget between full scans and artwork metadata.
// Waiting is cancellable and does not reserve future slots after cancellation.
func (b *Bridge) metadataSlot(ctx context.Context) error {
	select {
	case b.metadataGate <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	defer func() { <-b.metadataGate }()
	if err := waitContext(ctx, max(0, time.Until(b.metadataNext))); err != nil {
		return err
	}
	b.metadataNext = time.Now().Add(125 * time.Millisecond)
	return nil
}

type imageLoad struct {
	done  chan struct{}
	image *poster
	err   error
}

func (b *Bridge) image(ctx context.Context, path, tag string) (*poster, error) {
	key := imageRoute.FindStringSubmatch(path)[1] + ":" + tag
	b.mu.Lock()
	if load := b.imageFlights[key]; load != nil {
		b.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, fail(503, "artwork lookup timed out; retry shortly")
		case <-load.done:
			return load.image, load.err
		}
	}
	load := &imageLoad{done: make(chan struct{})}
	b.imageFlights[key] = load
	b.mu.Unlock()
	image, err := b.loadImage(ctx, path, tag)
	b.mu.Lock()
	load.image, load.err = image, err
	delete(b.imageFlights, key)
	close(load.done)
	b.mu.Unlock()
	return image, err
}

// Catalog artwork is viewed as the API key owner's primary profile. Silo
// enforces that profile's library access; no profile or catalog data is changed.
func (b *Bridge) libraryPoster(ctx context.Context, id string) error {
	contentID, err := decodeID("item", id)
	if err != nil {
		return err
	}
	profiles, err := b.cached(ctx, "/profiles", false)
	if err != nil {
		return err
	}
	profileID := ""
	for _, profile := range profiles {
		if yes(profile["is_primary"]) {
			profileID = str(profile["id"])
			break
		}
	}
	if profileID == "" {
		return fail(502, "library artwork requires the API key owner's primary Silo profile")
	}
	item, err := b.native(ctx, "GET", "/catalog/items/"+url.PathEscape(contentID), nil, nil, false, profileID)
	if err != nil {
		return err
	}
	// Episode stills are landscape; prefer the show's poster in Tracearr's
	// portrait slots, then the season poster, then the item's own artwork.
	refs := []any{item["poster_url"]}
	if str(item["type"]) == "episode" {
		refs = []any{item["series_poster_url"], item["season_poster_url"], item["poster_url"]}
	}
	for _, ref := range refs {
		if b.registerPoster(id, str(ref)) != "" {
			return nil
		}
	}
	return fail(404, "no supported library artwork available")
}
