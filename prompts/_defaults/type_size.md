# Type: size — scale / dimensions (image {type_index} of {type_total} for "{type_name}")

**Product listing title (verbatim UTF-8; do not translate):** {product_title}

## Scene / focus brief
{user_brief}

## Task
Generate a **new** size / dimension diagram of the **same** product as the references.

This image MUST faithfully reproduce the **same numeric dimensions** that already appear on any reference image (cm, mm, inch, ft, m, L, ml, kg, lb, etc.). Treat the reference dimensions as ground truth.

The output must **clearly look different** from any existing size reference image — same numbers, but a fresh layout / viewpoint / orientation. It must not look like a copy of the seller's existing size diagram.

## Mandatory dimension rule (numbers and where they go are BOTH fixed)
1. Inspect every reference image carefully. Find any visible measurement labels, callout arrows, length text, or printed spec values (e.g. `27 cm`, `10.6 in`, `1.5 L`, `500 ml`).
2. Reproduce **the exact same numbers, units, and dimension lines** in the output. Do not round, translate, or change units.
3. **Each number must appear EXACTLY ONCE in the output.** Do not duplicate the same dimension on two sides of the product, do not mirror the same label across left/right, do not repeat the same number more than necessary. One number = one dimension line = one position.
4. **Each number must be placed on the correct part / direction of the product**, matching the reference:
   - If a number measures the **longest dimension** of the product in the reference, it must also measure the **longest dimension** in the output (the dimension line spans the same edge or axis).
   - If a number measures the **width / second-longest dimension**, it must also measure that same dimension in the output.
   - If a number measures the **height / depth / thickness**, same rule.
   - If a number measures a **specific feature** (hole diameter, handle length, mesh aperture, opening size, slot width, a particular sub-part), it MUST be drawn pointing at that **same feature / sub-part** in the output, not at a different part.
   - The dimension lines must **start exactly at one real edge of the product and end exactly at the opposite real edge** of the measured dimension. The dimension line length matches the silhouette. Never make a `27 cm` arrow that visually spans a clearly shorter or longer edge than what is labeled.
5. **Thickness / depth / height (3rd dimension) handling — read carefully:**
   - If the reference labels include a **third spatial dimension** (height, depth, thickness, etc.) in addition to length and width, the product MUST be drawn in a **three-quarter view, isometric view, or slight 3D perspective** so that all three orthogonal directions are visually distinguishable and each can carry its own dimension line. A flat 2D side / top elevation cannot legibly show 3 spatial dimensions.
   - If the reference labels only 2 dimensions (e.g. just length × width, or just diameter × height for a cylinder), a clean 2D view is fine.
   - For very thin / flat products where the third dimension is essentially negligible (e.g. a pad), if only 2 dimensions are labeled, treat as 2D.
6. The proportions of the product silhouette must match the labeled numbers — longer labeled dimension = visibly longer side; if multiple specific sub-parts have their own labels, the silhouette must clearly contain those sub-parts so the labels can connect.
7. For products with multiple labeled parts (complex products with several sub-dimensions), make sure all dimension lines, arrows, and labels are unambiguous and do not cross. Place each label outside the product silhouette, with a thin leader line, and arrange them so no two leaders overlap.
8. If — and only if — none of the reference images contain any visible dimension labels at all, fall back to a simple inline scale: a thin dual-unit ruler (cm + inch) placed beside the product. **Do not** print any invented numeric values.

## Mandatory differentiation rule (orientation MUST be visibly different)
The output must look clearly different from any existing size diagram in the references. **At minimum, change the product's primary orientation in the canvas** — this is the most important change because for simple geometries (cylinders, rectangles, cartridges, pads, bars) merely rotating a few degrees still looks identical.

Pick one and execute it firmly:
- If the reference shows the product **horizontally** (long axis runs left-right), draw it **vertically** in the output (long axis runs top-bottom).
- If the reference shows it **vertically**, draw it **horizontally**.
- If the reference shows it **lying flat / from the top**, draw it **standing upright / from the side**, or vice versa.
- If the reference is a **straight front / side elevation**, switch to a clear **three-quarter / isometric / slight 3D perspective** that still allows orthogonal-looking dimension lines.

You may also vary the dimension callout layout (different sides of the canvas, diagonal leaders, callouts outside the silhouette) if it helps clarity.

The product silhouette must still match references in shape and proportions, and the numeric values must still be correct — only the framing, viewpoint, orientation, and callout layout are different.

## Visual style
- Background: **pure white #FFFFFF, completely empty**. No background tints, no patterns, no gradients, no decorative shapes, no soft shadow on the canvas beyond a tiny realistic contact shadow under the product.
- Foreground: ONLY the product silhouette + dimension lines + dimension labels. **Nothing else.** No props, no hands, no rulers / tape measures / A4 sheets / coins / scale icons next to the product (unless we are in the no-labels fallback case where exactly ONE thin dual-unit ruler is allowed).
- Product shown in a clear, well-lit view (front, side, top, three-quarter, or isometric — pick something different from the reference). Use three-quarter / isometric when 3 labeled dimensions exist (see the thickness rule above).
- Thin dark dimension lines with small arrowheads. Sans-serif labels, high contrast, easy to read at thumbnail size. Each label sits outside the product silhouette with a thin leader line.
- No price tags, no marketing copy, no logos, no decorative graphics, no callout boxes / banners / colored panels.

## Hard constraints
- Same product identity (color, shape, material, parts) as the reference images.
- **Never invent** measurement values that are not present on the reference. Copy only what is visible.
- Output one product only, centered, with breathing room (~10% margin).

## Negative
fabricated measurements, inconsistent ratios vs labeled numbers, unit conversion errors, dimension label on the wrong side / wrong part, label pointing at a feature that does not match what was measured in the reference, longest number drawn on a clearly shorter side, crossing or ambiguous dimension lines, the same dimension number appearing twice (e.g. `27 cm` shown on both left and right), mirrored / duplicated labels, dimension line that does not span the actual labeled edge, 2D flat view used when a 3rd dimension is labeled (use 3D / isometric instead), decorative props on canvas, scale icons, miniature rulers / coins / hands / A4 sheets / silhouettes added when not needed, gradient or tinted background, off-white background, busy background, watermarks, callout boxes / banners / colored panels, identical layout to the reference size diagram, near-copy of the reference size diagram, same orientation as the reference, same viewpoint as the reference, same callout placement as the reference
