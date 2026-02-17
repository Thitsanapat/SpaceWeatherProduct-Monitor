#!/usr/bin/env python3
"""Extract and dissolve Thailand boundary from provided GeoPackage into a shapefile.

Usage: run from repository root (virtualenv should have geopandas/fiona installed):
    python backend/scripts/extract_thailand_shapefile.py

This will write `backend/data/thailand_boundary/thailand_boundary.shp`.
"""
from pathlib import Path
import sys

GPkg = Path(__file__).resolve().parents[2] / 'frontend' / 'app' / 'tha_admbnda_adm1_rtsd_20190221.gpkg'
OUT_DIR = Path(__file__).resolve().parents[1] / 'data' / 'thailand_boundary'

def main():
    try:
        import geopandas as gpd
    except Exception as e:
        print("Missing required package geopandas:", e)
        print("Install with: pip install geopandas")
        sys.exit(2)

    # Prefer fiona for listing layers, fall back to pyogrio
    fiona = None
    pyogrio = None
    try:
        import fiona as _fiona
        fiona = _fiona
    except Exception:
        try:
            import pyogrio as _pyogrio
            pyogrio = _pyogrio
        except Exception:
            pass

    if not GPkg.exists():
        print(f"GeoPackage not found at: {GPkg}")
        sys.exit(1)

    if fiona is not None:
        layers = fiona.listlayers(str(GPkg))
        print("Found layers:", layers)
    elif pyogrio is not None:
        layers = pyogrio.list_layers(str(GPkg))
        print("Found layers:", layers)
    else:
        print("Neither fiona nor pyogrio available to list layers. Attempting to read default layer with geopandas.")
        layers = []

    # If we could not list layers, attempt to read default layer directly
    is_empty = False
    try:
        is_empty = (len(layers) == 0)
    except Exception:
        is_empty = False
    if is_empty:
        try:
            gdf = gpd.read_file(str(GPkg))
            print('Read default layer successfully')
        except Exception as e:
            print('Unable to read default layer:', e)
            sys.exit(4)
        # ensure geographic CRS
        try:
            gdf = gdf.to_crs(epsg=4326)
        except Exception:
            pass
        single = gdf.dissolve()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / 'thailand_boundary.shp'
        print(f"Writing shapefile to: {out_path}")
        single.to_file(str(out_path), driver='ESRI Shapefile')
        print('Done.')
        return

    # normalize layer names (some drivers return pairs like [name, geomtype])
    norm_layers = []
    for item in layers:
        try:
            name = item[0]
        except Exception:
            name = item
        norm_layers.append(str(name))

    # prefer layer names containing 'adm' or 'admbnda'
    layer = None
    for l in norm_layers:
        if 'adm' in l.lower() or 'admbnda' in l.lower():
            layer = l
            break
    if layer is None and norm_layers:
        layer = norm_layers[0]

    print(f"Reading layer: {layer}")
    print('layer type:', type(layer), 'repr:', repr(layer))
    try:
        gdf = gpd.read_file(str(GPkg), layer=layer)
    except Exception as e:
        print('Error reading layer with geopandas:', type(e).__name__, e)
        try:
            gdf = gpd.read_file(str(GPkg), layer=str(layer))
            print('Read with str(layer) succeeded')
        except Exception as e2:
            print('Second attempt failed:', type(e2).__name__, e2)
            raise
    if hasattr(gdf, 'empty') and gdf.empty:
        print("Layer appears empty")
        sys.exit(3)

    # Ensure geographic CRS for shapefile (WGS84)
    try:
        gdf = gdf.to_crs(epsg=4326)
    except Exception:
        pass

    # Dissolve all features into a single geometry (national boundary)
    single = gdf.dissolve()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / 'thailand_boundary.shp'

    print(f"Writing shapefile to: {out_path}")
    single.to_file(str(out_path), driver='ESRI Shapefile')
    print("Done.")


if __name__ == '__main__':
    main()
