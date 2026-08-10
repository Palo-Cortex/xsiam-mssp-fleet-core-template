# ExamplePack

Exemplar MSSP-authored content pack for the fleet template. It carries one
automation script (`ExampleScript`) and exists so the template's release →
pin → converge pipeline has a real pack to exercise end to end:

- A push to `main` touching this pack tags `ExamplePack-v<version>` and
  publishes the pack zip as a GitHub Release (`release.yml`).
- The released version is pinned per ring in `fleet/pins/<ring>.yml` and
  listed in `mssp_pack_catalog.json`.
- Converge installs the pinned zip on every tenant that composes the pack.

When adopting the template, replace this pack with your own MSSP-authored
content (see `docs/getting-started.md`), or delete it along with its pin and
catalog entries.
