# Catalog fixtures

These fixtures are minimal synthetic records. Resource IDs, names and CDN paths
are fictional. They contain no authentication material or captured response
bodies, and their URLs are not download targets.

- `album.json`: release artwork alongside a larger editorial square.
- `artist.json`: artist banner and motion fields. Preview dimensions describe
  the image and do not establish the HLS video dimensions.
- `map.json`: a resource reference, a dotted playlist ID and a separately owned
  song. The playlist omits redundant ID/type fields to test map-key ownership.
- `../../examples/response.json`: an offline CLI example covering editorial
  fields, semantic aliases, a motion preview, HLS query preservation and a related
  song whose audio preview is excluded.
- `../test_core_v02.py`: in-memory records for storefront conflicts,
  field-presence states, wide hero selection, same-URL aliases with conflicting
  P3 declarations, malformed inputs and candidate-scan boundaries.

The fixtures express parser and selection behavior without requesting Apple
services. With the development and motion dependencies installed, run the offline
suite from the repository root:

```sh
python -B -m unittest discover -s tests -v
```

See [CONTRIBUTING](../../CONTRIBUTING.md) for dependency combinations and separate
media-tool integration checks.
