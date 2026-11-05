package main

import (
	"context"
	"net/url"
)

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
