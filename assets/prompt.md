# Logo Generation Prompt

Generated with `gemimg` (Gemini image generation) using the
`gemini-3.1-flash-image-preview` model.

## Concept

A lowercase 't' constructed from plant elements:

- The **crossbar** is formed by two leaves pointing left and right
- A **leaf-bud** points upward from the top of the stem
- The **stem** curves at the bottom

The letter reads simultaneously as a 't' (for "tend") and a seedling — the
structural elements of the letter *are* leaves, not a letter decorated with
leaves.

## Color

Pale oak / sand (`#b5a48a`) on white — inspired by John Pawson's material
palette (limestone, pale wood, raw plaster). Warm, muted, natural.

## Prompt

```
gemimg "Minimalist logo for a developer tool called tend. A lowercase 't' made
from plant elements: the crossbar is formed by two leaves pointing left and
right, a leaf-bud points upward from the top of the stem, and the stem curves
at the bottom. Use a pale oak/sand color (#b5a48a) on a pure white background.
Clean flat design, filled shapes, no gradients. Quiet, serene, minimal. No
other text." --model gemini-3.1-flash-image-preview -o logo.png --aspect-ratio 1:1
```

## Sizes

Resized with `sips -Z <size> logo.png --out logo-<size>.png`:

| File | Size | Use |
|------|------|-----|
| `logo-1024.png` | 1024px | Full resolution source |
| `logo-512.png` | 512px | GitHub avatar, social previews |
| `logo-256.png` | 256px | README badges, docs |
| `logo-128.png` | 128px | Small icons |
| `logo-64.png` | 64px | Favicons |
| `logo-32.png` | 32px | Favicons |
