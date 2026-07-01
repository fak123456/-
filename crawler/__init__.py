"""Standalone Amazon product crawler.

Reads an Excel file whose column A holds Amazon product URLs, scrapes title
and hi-res reference images per product, and packages each product into
``{ASIN}.zip`` in a layout compatible with the existing image-generator's
``gui/zip_util.extract_product_zip``. Also writes a 2-column ``商品列表.xlsx``
ready to be dragged into the generator's batch import tab.

This package is fully self-contained: it does not import any other code from
the parent project, and the parent project does not import from here. Run it
with ``python -m crawler.run --input urls.xlsx --out crawler/output``.
"""
