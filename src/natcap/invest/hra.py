"""Habitat risk assessment (HRA) model for InVEST."""
from __future__ import absolute_import
import os
import logging

import pickle
import numpy
from osgeo import gdal, ogr, osr
import pandas
import pygeoprocessing
import shapely.wkt
import taskgraph

from . import utils
from . import validation

LOGGER = logging.getLogger('natcap.invest.hra')

# Parameters from the user-provided CSV files
_SUBREGION_FIELD_NAME = 'name'
_BUFFER_HEADER = 'STRESSOR BUFFER (METERS)'
_CRITERIA_TYPE_HEADER = 'CRITERIA TYPE'
_SPATIAL_CRITERIA_TYPE = 'spatial_criteria'
_HABITAT_NAME_HEADER = 'HABITAT NAME'
_HABITAT_RESILIENCE_HEADER = 'HABITAT RESILIENCE ATTRIBUTES'
_HABITAT_STRESSOR_OVERLAP_HEADER = 'HABITAT STRESSOR OVERLAP PROPERTIES'

# Parameters for the spatially explicit criteria shapefiles
_RATING_FIELD = 'rating'

# A cutoff for the decay amount after which we will say scores are equivalent
# to 0, since we don't want to have values outside the buffer zone.
_EXP_DEDAY_CUTOFF = 1E-6

# Target cell type or values for raster files.
_TARGET_FLT_PIXEL = gdal.GDT_Float32
_TARGET_INT_PIXEL = gdal.GDT_Byte
_TARGET_NODATA_FLT = float(numpy.finfo(numpy.float32).min)
_TARGET_NODATA_INT = 255  # for unsigned 8-bit int


def execute(args):
    """InVEST Habitat Risk Assessment (HRA) Model.

    Parameters:
        args['workspace_dir'] (str): a path to the output workspace folder.
            It will overwrite any files that exist if the path already exists.

        args['results_suffix'] (str): a string appended to each output file
            path. (optional)

        args['info_csv_path'] (str): a path to the CSV file that contains
            the name of the habitat (H) or stressor (s) on the `NAME` column
            that matches the names in criteria_csv_path. Each H/S has
            its corresponding vector or raster path on the `PATH` column. The
            `STRESSOR BUFFER (meters)` column should have a buffer value if
            the `TYPE` column is a stressor.

        args['criteria_csv_path'] (str): a path to the CSV file that
            contains the set of criteria ranking of each stressor on each
            habitat.

        args['resolution'] (int): a number representing the desired pixel
            dimensions of output rasters in meters.

        args['max_rating'] (str, int or float): a number representing the
            highest potential value that should be represented in rating in the
            criteria scores CSV table.

        args['risk_eq'] (str): a string identifying the equation that should be
            used in calculating risk scores for each H-S overlap cell. This
            will be either 'Euclidean' or 'Multiplicative'.

        args['decay_eq'] (str): a string identifying the equation that should
            be used in calculating the decay of stressor buffer influence. This
            can be 'None', 'Linear', or 'Exponential'.

        args['aoi_vector_path'] (str): a path to the shapefile containing one
            or more planning regions used to get the average risk value for
            each habitat-stressor combination over each area. Optionally, if
            each of the shapefile features contain a 'name' field, it will
            be used as a way of identifying each individual shape.

        args['n_workers'] (int): the number of worker processes to
            use for processing this model.  If omitted, computation will take
            place in the current process. (optional)

    Returns:
        None.

    """
    LOGGER.info('Validating arguments')
    invalid_parameters = validate(args)
    if invalid_parameters:
        raise ValueError("Invalid parameters passed: %s" % invalid_parameters)

    # Validate and store inputs
    LOGGER.info('Validating criteria CSV file and return cleaned dataframe.')
    criteria_df = _get_criteria_dataframe(args['criteria_csv_path'])

    # Create initial working directories and determine file suffixes
    intermediate_dir = os.path.join(
        args['workspace_dir'], 'intermediate_outputs')
    file_preprocessing_dir = os.path.join(
        intermediate_dir, 'file_preprocessing')
    output_dir = os.path.join(args['workspace_dir'], 'outputs')
    utils.make_directories(
        [output_dir, intermediate_dir, file_preprocessing_dir])
    file_suffix = utils.make_suffix_string(args, 'results_suffix')

    # Initialize a TaskGraph
    work_token_dir = os.path.join(intermediate_dir, '_tmp_work_tokens')
    try:
        n_workers = int(args['n_workers'])
    except (KeyError, ValueError, TypeError):
        # KeyError when n_workers is not present in args
        # ValueError when n_workers is an empty string.
        # TypeError when n_workers is None.
        n_workers = -1  # single process mode.
    graph = taskgraph.TaskGraph(work_token_dir, n_workers)

    # Calculate recovery for each habitat, and overlap scores for each
    # habitat-stressor, and store data in the dataframes
    info_df, habitat_names, stressor_names = _get_info_dataframe(
        args['info_csv_path'], file_preprocessing_dir, intermediate_dir,
        output_dir, file_suffix)
    resilience_attributes, stressor_attributes = \
        _get_attributes_from_df(criteria_df, habitat_names, stressor_names)
    stressor_names = stressor_attributes.keys()
    max_rating = float(args['max_rating'])
    recovery_df = _get_recovery_dataframe(
        criteria_df, habitat_names, resilience_attributes, max_rating,
        file_preprocessing_dir, output_dir, file_suffix)
    overlap_df = _get_overlap_dataframe(
        criteria_df, habitat_names, stressor_attributes, max_rating,
        file_preprocessing_dir, intermediate_dir, file_suffix)

    # Append spatially explicit criteria rasters to info_df
    criteria_csv_dir = os.path.dirname(args['criteria_csv_path'])
    info_df = _append_spatial_raster_row(
        info_df, recovery_df, overlap_df, criteria_csv_dir,
        file_preprocessing_dir, file_suffix)

    # Get target projection from the AOI vector file
    if 'aoi_vector_path' in args and args['aoi_vector_path'] != '':
        target_sr_wkt = pygeoprocessing.get_vector_info(
            args['aoi_vector_path'])['projection']
        target_sr = osr.SpatialReference()
        if target_sr_wkt:
            target_sr.ImportFromWkt(target_sr_wkt)

        if not target_sr.IsProjected():
            raise ValueError(
                'The AOI vector file %s is provided but not projected.' %
                args['aoi_vector_path'])
        else:
            # Get the value to multiply by linear distances in order to
            # transform them to meters
            linear_unit = target_sr.GetLinearUnits()
            LOGGER.info(
                'Target projection from subregion: %s. EPSG: %s. Linear unit: '
                '%s.' % (target_sr.GetAttrValue('PROJECTION'),
                         target_sr.GetAttrValue("AUTHORITY", 1), linear_unit))

    # Simplify the AOI vector for faster run on zonal statistics
    simplified_aoi_vector_path = os.path.join(
        file_preprocessing_dir, 'simplified_aoi%s.gpkg' % file_suffix)
    aoi_tolerance = (float(args['resolution']) / linear_unit) / 2

    # Check if subregion field exists in the AOI vector
    subregion_field_exists = _has_field_name(
        args['aoi_vector_path'], _SUBREGION_FIELD_NAME)

    # Simplify the AOI and preserve the subregion field if it exists
    LOGGER.info('Simplifying the AOI vector.')
    aoi_preserved_field = None
    if subregion_field_exists:
        aoi_preserved_field = (_SUBREGION_FIELD_NAME, ogr.OFTString)

    simplify_aoi_task = graph.add_task(
        func=_simplify_geometry,
        args=(args['aoi_vector_path'], aoi_tolerance,
              simplified_aoi_vector_path),
        kwargs={'preserved_field': aoi_preserved_field},
        target_path_list=[simplified_aoi_vector_path],
        task_name='simplify_aoi_vector')

    # Use the simplified AOI vector to run analysis on
    aoi_vector_path = simplified_aoi_vector_path

    # Merge geometries in the simplified AOI if subregion field doesn't exist
    if not subregion_field_exists:
        merged_aoi_vector_path = os.path.join(
            file_preprocessing_dir, 'merged_aoi.gpkg')
        LOGGER.info('Merging the geometries from the AOI vector.')
        merge_aoi_task = graph.add_task(
            func=_merge_geometry,
            args=(simplified_aoi_vector_path, merged_aoi_vector_path),
            target_path_list=[merged_aoi_vector_path],
            task_name='merge_aoi_vector',
            dependent_task_list=[simplify_aoi_task])

        # Use the merged AOI vector to run analysis on
        aoi_vector_path = merged_aoi_vector_path

    # Rasterize habitat and stressor layers if they are vectors.
    # Divide resolution (meters) by linear unit to convert to projection units
    target_pixel_size = (float(args['resolution'])/linear_unit,
                         -float(args['resolution'])/linear_unit)

    # Create a raster from vector extent with 0's, then burn the vector
    # onto the raster with 1's, for all the H/S layers that are not a raster
    for _, row in info_df.iterrows():
        if not row['IS_RASTER']:
            vector_name = row['NAME']
            vector_type = row['TYPE']
            vector_path = row['PATH']
            simplified_vector_path = row['SIMPLE_VECTOR_PATH']
            tolerance = (float(args['resolution']) / row['LINEAR_UNIT']) / 2
            target_raster_path = row['BASE_RASTER_PATH']
            LOGGER.info('Rasterizing %s.' % vector_name)

            # Simplify the vector geometry first, with a tolerance of half the
            # target resolution
            simplify_geometry_task = graph.add_task(
                func=_simplify_geometry,
                args=(vector_path, tolerance, simplified_vector_path),
                kwargs={'preserved_field': (_RATING_FIELD, ogr.OFTReal)},
                target_path_list=[simplified_vector_path],
                task_name='simplify_%s_vector' % vector_name)

            if vector_type == _SPATIAL_CRITERIA_TYPE:
                # If it's a spatial criteria vector, burn the values from the
                # `rating` attribute
                rasterize_kwargs = {
                    'option_list': ["ATTRIBUTE=" + _RATING_FIELD]}
                rasterize_nodata = _TARGET_NODATA_FLT
                rasterize_pixel_type = _TARGET_FLT_PIXEL

            else:  # Could be a habitat or stressor vector
                # Fill the raster with 1s on where a vector geometry exists
                rasterize_kwargs = {'burn_values': [1],
                                    'option_list': ["ALL_TOUCHED=TRUE"]}
                rasterize_nodata = _TARGET_NODATA_INT
                rasterize_pixel_type = _TARGET_INT_PIXEL

            # Create raster from the simplified vector and fill with 0s
            create_raster_task = graph.add_task(
                func=pygeoprocessing.create_raster_from_vector_extents,
                args=(simplified_vector_path, target_raster_path,
                      target_pixel_size, rasterize_pixel_type, rasterize_nodata),
                kwargs={'fill_value': 0},
                target_path_list=[target_raster_path],
                task_name='create_raster_from_%s' % vector_name,
                dependent_task_list=[simplify_geometry_task])

            rasterize_task = graph.add_task(
                func=pygeoprocessing.rasterize,
                args=(simplified_vector_path, target_raster_path),
                kwargs=rasterize_kwargs,
                target_path_list=[target_raster_path],
                task_name='rasterize_%s' % vector_name,
                dependent_task_list=[create_raster_task])

    # Join the raster creation tasks first, since align_and_resize_rasters_task
    # is dependent on them.
    graph.join()

    # Align and resize all the rasters, including rasters provided by the user,
    # and rasters created from the vectors.
    base_raster_list = info_df.BASE_RASTER_PATH.tolist()
    align_raster_list = info_df.ALIGN_RASTER_PATH.tolist()

    LOGGER.info('Starting align_and_resize_raster_task.')
    align_and_resize_rasters_task = graph.add_task(
        func=pygeoprocessing.align_and_resize_raster_stack,
        args=(base_raster_list,
              align_raster_list,
              ['near'] * len(base_raster_list),
              target_pixel_size, 'union'),
        kwargs={'target_sr_wkt': target_sr_wkt},
        target_path_list=align_raster_list,
        task_name='align_and_resize_raster_task')

    # Join here since everything below requires aligned and resized rasters
    graph.join()

    # Make buffer stressors based on their impact distance and decay function
    align_stressor_raster_list = info_df[
        info_df.TYPE == 'stressor'].ALIGN_RASTER_PATH.tolist()
    dist_stressor_raster_list = info_df[
        info_df.TYPE == 'stressor'].DIST_RASTER_PATH.tolist()
    stressor_names = info_df[info_df.TYPE == 'stressor'].NAME.tolist()

    LOGGER.info('Calculating euclidean distance transform on stressors.')
    # Convert pixel size from meters to projection unit
    sampling_distance = (float(args['resolution'])/linear_unit,
                         float(args['resolution'])/linear_unit)
    distance_transform_task_list = []
    for (align_raster_path, dist_raster_path, stressor_name) in zip(
         align_stressor_raster_list, dist_stressor_raster_list,
         stressor_names):

        distance_transform_task_list.append(graph.add_task(
            func=pygeoprocessing.distance_transform_edt,
            args=((align_raster_path, 1), dist_raster_path),
            kwargs={'sampling_distance': sampling_distance,
                    'working_dir': intermediate_dir},
            target_path_list=[dist_raster_path],
            task_name='distance_transform_%s' % stressor_name))

    LOGGER.info('Calculating maximum overlapping stressors on the ecosystem.')
    align_habitat_raster_list = info_df[
        info_df.TYPE == 'habitat'].ALIGN_RASTER_PATH.tolist()
    overlap_stressor_raster_path = os.path.join(
        file_preprocessing_dir, 'stressor_overlap%s.tif' % file_suffix)
    ecosystem_raster_path = os.path.join(
        file_preprocessing_dir, 'ecosystem_abundance%s.tif' % file_suffix)
    max_risk_score = _get_max_risk_score(
        align_stressor_raster_list, align_habitat_raster_list,
        overlap_stressor_raster_path, ecosystem_raster_path, max_rating,
        args['risk_eq'])

    # For each habitat, calculate the individual and culmulative exposure,
    # consequence, and risk scores from each stressor.
    for i, habitat in enumerate(habitat_names):
        LOGGER.info('Calculating recovery scores on habitat %s.' % habitat)
        # On a habitat raster, a pixel value of 0 indicates the existence of
        # habitat, whereas 1 means non-existence.
        habitat_raster_path = info_df.loc[
            info_df.NAME == habitat, 'ALIGN_RASTER_PATH'].item()
        graph.add_task(
            func=_calc_habitat_recovery,
            args=(habitat_raster_path, habitat, recovery_df, max_rating),
            target_path_list=[
                recovery_df.loc[habitat, path] for path in [
                    'R_RASTER_PATH', 'R_NUM_RASTER_PATH']],
            task_name='calculate_%s_recovery' % habitat)

        # Calculate exposure/consequence scores on each stressor-habitat pair
        for j, stressor in enumerate(stressor_names):
            LOGGER.info('Calculating exposure, consequence, and risk scores '
                        'from stressor %s to habitat %s.' % (stressor, habitat))

            stressor_dist_raster_path = info_df.loc[
                info_df.NAME == stressor, 'DIST_RASTER_PATH'].item()

            # Convert stressor buffer from meters to projection unit
            stressor_buffer = info_df.loc[
                info_df.NAME == stressor, _BUFFER_HEADER].item() / float(
                    info_df.loc[info_df.NAME == stressor, 'LINEAR_UNIT'].item())

            # Calculate exposure scores on each habitat-stressor pair
            expo_dependent_task_list = [distance_transform_task_list[j]]
            _calc_pair_criteria_score(
                overlap_df.loc[(habitat, stressor)], habitat_raster_path,
                stressor_dist_raster_path, stressor_buffer, args['decay_eq'],
                'E', graph, expo_dependent_task_list)

            # Calculate consequence scores on each habitat-stressor pair.
            # Add recovery numerator and denominator to the scores
            conseq_dependent_task_list = [distance_transform_task_list[j]]
            _calc_pair_criteria_score(
                overlap_df.loc[(habitat, stressor)], habitat_raster_path,
                stressor_dist_raster_path, stressor_buffer, args['decay_eq'],
                'C', graph, conseq_dependent_task_list)

            # Calculate pairwise habitat-stressor risks.
            pair_e_raster_path, pair_c_raster_path, \
                target_pair_risk_raster_path = [
                    overlap_df.loc[(habitat, stressor), path] for path in
                    ['E_RASTER_PATH', 'C_RASTER_PATH', 'PAIR_RISK_RASTER_PATH']]
            pair_risk_calculation_list = [
                (pair_e_raster_path, 1), (pair_c_raster_path, 1),
                (args['risk_eq'], 'raw')]

            graph.add_task(
                func=pygeoprocessing.raster_calculator,
                args=(pair_risk_calculation_list, _pair_risk_op,
                      target_pair_risk_raster_path, _TARGET_FLT_PIXEL,
                      _TARGET_NODATA_FLT),
                target_path_list=[target_pair_risk_raster_path],
                task_name='calculate_%s_%s_risk' % (habitat, stressor),
                dependent_task_list=conseq_dependent_task_list +
                expo_dependent_task_list)

        graph.join()

        # Calculate cumulative E, C & risk scores on each habitat
        final_e_habitat_path, final_c_habitat_path = [
            info_df.loc[info_df.NAME == habitat, column_header].item() for
            column_header in ['FINAL_E_RASTER_PATH', 'FINAL_C_RASTER_PATH']]

        LOGGER.info(
            'Calculating total exposure scores on habitat %s.' % habitat)
        e_num_path_const_list = [
            (path, 1) for path in
            overlap_df.loc[habitat, 'E_NUM_RASTER_PATH'].tolist()]
        e_denom_list = [
            (denom, 'raw') for denom in
            overlap_df.loc[habitat, 'E_DENOM'].tolist()]

        final_e_path_band_list = [(habitat_raster_path, 1)] + \
            e_num_path_const_list + e_denom_list

        # Calculate total exposure on the habitat
        graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=(final_e_path_band_list,
                  _final_expo_score_op,
                  final_e_habitat_path,
                  _TARGET_FLT_PIXEL,
                  _TARGET_NODATA_FLT),
            target_path_list=[final_e_habitat_path],
            task_name='calculate_total_exposure_%s' % habitat)

        LOGGER.info(
            'Calculating total consequence scores on habitat %s.' % habitat)
        recov_num_raster_path = recovery_df.loc[habitat, 'R_NUM_RASTER_PATH']
        c_num_path_const_list = [
            (path, 1) for path in
            overlap_df.loc[habitat, 'C_NUM_RASTER_PATH'].tolist()]
        c_denom_list = [
            (denom, 'raw') for denom in
            overlap_df.loc[habitat, 'C_DENOM'].tolist()]
        c_denom_list.append((recovery_df.loc[habitat, 'R_DENOM'], 'raw'))

        final_c_path_const_list = [
            (habitat_raster_path, 1), (recov_num_raster_path, 1)] + \
            c_num_path_const_list + c_denom_list

        # Calculate total consequence on the habitat
        graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=(final_c_path_const_list,
                  _final_conseq_score_op,
                  final_c_habitat_path,
                  _TARGET_FLT_PIXEL,
                  _TARGET_NODATA_FLT),
            target_path_list=[final_c_habitat_path],
            task_name='calculate_total_consequence_%s' % habitat)

        LOGGER.info('Calculating total risk score and reclassified risk scores'
                    ' on habitat %s.' % habitat)
        total_habitat_risk_path, reclass_habitat_risk_path = [
            info_df.loc[info_df.NAME == habitat, column_header].item() for
            column_header in [
                'TOTAL_RISK_RASTER_PATH', 'RECLASS_RISK_RASTER_PATH']]

        # Get a list of habitat path and individual risk paths on that habitat
        # for the final risk calculation
        total_risk_path_band_list = [(habitat_raster_path, 1)]
        pair_risk_path_list = overlap_df.loc[
            habitat, 'PAIR_RISK_RASTER_PATH'].tolist()
        total_risk_path_band_list = total_risk_path_band_list + [
            (path, 1) for path in pair_risk_path_list]

        # Calculate the cumulative risk on the habitat from all stressors
        calc_risk_task = graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=(total_risk_path_band_list, _tot_risk_op,
                  total_habitat_risk_path, _TARGET_FLT_PIXEL,
                  _TARGET_NODATA_FLT),
            target_path_list=[total_habitat_risk_path],
            task_name='calculate_%s_risk' % habitat)

        # Calculate the risk score on a reclassified basis by dividing the risk
        # score by the maximum possible risk score.
        graph.add_task(
            func=pygeoprocessing.raster_calculator,
            args=([(total_habitat_risk_path, 1), (max_risk_score, 'raw')],
                  _reclassify_risk_op, reclass_habitat_risk_path,
                  _TARGET_FLT_PIXEL, _TARGET_NODATA_FLT),
            target_path_list=[reclass_habitat_risk_path],
            task_name='reclassify_%s_risk' % habitat,
            dependent_task_list=[calc_risk_task])

    # Calculate ecosystem risk scores. This task depends on every task above,
    # so join the graph first.
    graph.join()
    LOGGER.info('Calculating ecosystem risk.')

    # Create input list for calculating reclassified ecosystem risk.
    ecosystem_risk_raster_path = os.path.join(output_dir, 'risk_ecosystem.tif')
    hab_risk_path_list = info_df.loc[info_df.TYPE == 'habitat'][
        'TOTAL_RISK_RASTER_PATH'].tolist()
    hab_risk_path_band_list = [
        (ecosystem_raster_path, 1), (max_risk_score, 'raw')]
    # Append individual habitat risk rasters to the list
    for path in hab_risk_path_list:
        hab_risk_path_band_list.append((path, 1))

    # Calculate and reclassify ecosystem risk
    graph.add_task(
        func=pygeoprocessing.raster_calculator,
        args=(hab_risk_path_band_list, _ecosystem_risk_op,
              ecosystem_risk_raster_path, _TARGET_FLT_PIXEL,
              _TARGET_NODATA_FLT),
        target_path_list=[ecosystem_risk_raster_path],
        task_name='calculate_ecosystem_risk')

    # Calculate the mean criteria scores on the habitat pixels within the
    # polygons in the AOI vector
    LOGGER.info('Calculating zonal statistics.')
    stats_df = _get_zonal_stats(overlap_df, aoi_vector_path, graph)

    # Convert the statistics dataframe to a CSV file
    stats_csv_path = os.path.join(
        output_dir, 'criteria_score_stats%s.csv' % file_suffix)
    stats_df.to_csv(stats_csv_path)

    # Unproject output rasters to WGS84 (World Mercator), and then convert
    # the rasters to GeoJSON files for visualization
    LOGGER.info('Unprojecting output rasters')
    out_risk_raster_paths = info_df[
        info_df.TYPE == 'habitat'].RECLASS_RISK_RASTER_PATH.tolist()
    out_recov_raster_paths = recovery_df.R_RASTER_PATH.tolist()
    out_raster_paths = out_risk_raster_paths + out_recov_raster_paths \
        + [ecosystem_risk_raster_path]

    wgs84_raster_paths = [
        os.path.join(file_preprocessing_dir, 'wgs84_' + os.path.basename(path))
        for path in out_raster_paths]

    # Use WGS84 to convert meter coordinates back to lat/lon, since only this
    # format would be recognized by Leaflet
    wgs84_sr = osr.SpatialReference()
    wgs84_sr.ImportFromEPSG(4326)
    wgs84_wkt = wgs84_sr.ExportToWkt()

    # Convert meter resolution to lat/long pixel size
    # See https://gis.stackexchange.com/questions/2951/algorithm-for-offsetting-a-latitude-longitude-by-some-amount-of-meters
    wgs84_pixel_size = (float(args['resolution'])/111111,
                        -float(args['resolution'])/111111)

    # Unproject the rasters to WGS84
    unproject_task = graph.add_task(
        func=pygeoprocessing.align_and_resize_raster_stack,
        args=(out_raster_paths, wgs84_raster_paths,
              ['near'] * len(out_raster_paths), wgs84_pixel_size, 'union'),
        kwargs={'target_sr_wkt': wgs84_wkt},
        target_path_list=wgs84_raster_paths,
        task_name='reproject_risk_rasters_to_wgs84')

    # Convert the unprojected rasters to GeoJSON files for web visualization
    for raster_path in wgs84_raster_paths:
        # Remove the `wgs84_` prefix from the output GeoJSON file names
        vector_layer_name = os.path.splitext(
            os.path.basename(raster_path))[0].replace('wgs84_', '')
        vector_path = os.path.join(
            output_dir, vector_layer_name.replace('wgs84_', '') + '.geojson')

        if vector_layer_name.startswith('risk_'):
            field_name = 'Risk Score'
        else:
            field_name = 'Recovery Potential'

        graph.add_task(
            func=_raster_to_geojson,
            args=(raster_path, vector_path, vector_layer_name, field_name),
            target_path_list=[vector_path],
            task_name='create_%s_geojson' % vector_layer_name,
            dependent_task_list=[unproject_task])

    graph.join()
    graph.close()


def _raster_to_geojson(
        base_raster_path, target_geojson_path, layer_name, field_name):
    """Convert a raster to a GeoJSON file with layer and field name.

    The GeoJSON file will be in the shape and projection of the raster.

    Parameters:
        base_raster_path (str): the raster that needs to be turned into a
            GeoJSON file.

        target_geojson_path (str): the desired path for the new GeoJSON.

        layer_name (str): the name of the layer going into the new shapefile.

        field_name (str): the name of the field to write raster values in.

    Returns:
        None.

    """
    raster = gdal.OpenEx(base_raster_path)
    band = raster.GetRasterBand(1)
    mask = band.GetMaskBand()

    driver = ogr.GetDriverByName('GeoJSON')
    vector = driver.CreateDataSource(target_geojson_path)

    # Use raster projection wkt for the GeoJSON
    sr = osr.SpatialReference()
    sr.ImportFromWkt(raster.GetProjectionRef())

    layer_name = layer_name.encode('utf-8')
    vector_layer = vector.CreateLayer(layer_name, sr, ogr.wkbPolygon)

    # Create an integer field that contains risk scores 0 to 3 from the raster
    field_defn = ogr.FieldDefn(field_name, ogr.OFTInteger)
    field_defn.SetWidth(2)
    field_defn.SetPrecision(0)
    vector_layer.CreateField(field_defn)

    gdal.Polygonize(band, mask, vector_layer, 0)

    band = None
    raster = None
    vector.SyncToDisk()
    vector_layer = None
    vector = None


def _get_and_pickle_zonal_stats(
        criteria_raster_path, aoi_vector_path, fid_name_dict,
        target_pickle_path):
    """Write zonal stats to the dataframe and pickle them to files.

    Parameters:
        criteria_raster_path (str): the path to the criteria score raster
            to be analyzed.

        aoi_vector_path (str): a path to a vector containing one or more
            features to calculate statistics over.

        fid_name_dict (dict): a dictionary of fid key and feature name value
            for converting fid in zonal_stats_dict to feature name.

        target_pickle_path (str): a path to the pickle file for storing zonal
            statistics.

    Returns:
        None.

    """
    zonal_stats_dict = pygeoprocessing.zonal_statistics(
        (criteria_raster_path, 1), aoi_vector_path)

    # Create a stats dict that has mean scores calculated from zonal stats.
    # Use the name of the subregion as key of mean_stats_dict.
    mean_stats_dict = {}
    for fid, stats in zonal_stats_dict.iteritems():
        # 0 indicates no overlap between the habitat and stressor in
        # the subregion or AOI
        region_name = fid_name_dict[fid]
        mean_stats_dict[region_name] = 0

        # If there's overlap between habitat and stressor
        if stats['count'] > 0:
            # Calculate the mean score by dividing the sum of scores by the
            # count of pixel in that subregion
            mean_stats_dict[region_name] = stats['sum']/stats['count']

    pickle.dump(mean_stats_dict, open(target_pickle_path, 'wb'))


def _get_zonal_stats(overlap_df, aoi_vector_path, graph):
    """Get zonal stats for stressor-habitat pair and ecosystem as dataframe.

    Add each zonal stats calculation to Taskgraph to allow parallel processing,
    and pickle the stats dictionary to the output_dir.

    Parameters:
        overlap_df (dataframe): a multi-index dataframe with exposure and
            consequence raster paths, as well as stats columns for writing
            zonal statistics dictionary on.

        aoi_vector_path (str): a path to a vector containing one or more
            features to calculate statistics over.

        graph (Taskgraph object): an object for building task graphs and
            parallelizing independent tasks.

    Returns:
        stats_df (dataframe): a multi-index dataframe with exposure and
            consequence mean score columns. Each cell value would have a
            dictionary whose keys are subregion names from AOI vector and
            values the mean score. A score of 0  means that there's no
            overlapped pixel for the habitat-stressor pair.

    """
    # Get fid and the name of each feature in the AOI vector
    aoi_vector = gdal.OpenEx(aoi_vector_path, gdal.OF_VECTOR)
    aoi_layer = aoi_vector.GetLayer()
    fid_name_dict = {}
    for aoi_feature in aoi_layer:
        fid = aoi_feature.GetFID()
        aoi_layer_defn = aoi_layer.GetLayerDefn()

        # If AOI has the subregion field, use that field to get subregion names
        subregion_field_idx = aoi_layer_defn.GetFieldIndex(
            _SUBREGION_FIELD_NAME)
        if subregion_field_idx != -1:
            field_name = aoi_feature.GetField(subregion_field_idx)
            fid_name_dict[fid] = field_name
        # If AOI doesn't have subregion field, use `AOI` to represent the whole
        # area of interest
        else:
            fid_name_dict[fid] = 'AOI'

    aoi_layer = None
    aoi_vector = None

    # Compute zonal criteria scores on each habitat-stressor pair within AOI
    for hab_str_idx, row in overlap_df.iterrows():
        for criteria_type in ['E', 'C']:
            criteria_raster_path = row[criteria_type + '_RASTER_PATH']
            target_pickle_path = row[criteria_type + '_PICKLE_STATS_PATH']

            # Get habitat-stressor name without extension
            habitat_stressor = '_'.join(hab_str_idx)

            # Calculate and pickle zonal stats to files
            graph.add_task(
                func=_get_and_pickle_zonal_stats,
                args=(criteria_raster_path, aoi_vector_path, fid_name_dict,
                      target_pickle_path),
                target_path_list=[target_pickle_path],
                task_name='_get_and_pickle_%s_zonal_stats' % habitat_stressor)

    # Join first to get all the result statistics
    graph.join()

    # Load zonal stats from a pickled file and write it to the dataframe
    for criteria_type in ['E', 'C']:
        overlap_df[criteria_type + '_MEAN'] = overlap_df.apply(
            lambda row: pickle.load(
                open(row[criteria_type + '_PICKLE_STATS_PATH'], 'rb')), axis=1)

    # Extract criteria mean columns to new dataframe stats_df as a copy

    stats_df = overlap_df.filter(['E_MEAN', 'C_MEAN'], axis=1)
    return stats_df


def _merge_geometry(base_vector_path, target_merged_vector_path):
    """Merge geometries from base vector into target vector.

    Parameters:
        base_vector_path (str): a path to the vector with geometries going to
            be merged.

        target_merged_vector_path (str): a path to the target vector to write
            merged geometries at.

    Returns:
        None

    """

    base_vector = gdal.OpenEx(base_vector_path, gdal.OF_VECTOR)
    base_layer = base_vector.GetLayer()
    shapely_geoms = []

    for feat in base_layer:
        geom = feat.GetGeometryRef()
        geom_wkt = shapely.wkt.loads(geom.ExportToWkt())
        # Buffer geometry to prevent invalid geometries
        geom_buffered = geom_wkt.buffer(0)
        shapely_geoms.append(geom_buffered)

    # Return the union of the geometries in the list
    merged_geom = shapely.ops.unary_union(shapely_geoms)

    # Create a new geopackage
    target_driver = ogr.GetDriverByName('GPKG')
    target_vector = target_driver.CreateDataSource(target_merged_vector_path)

    # Get basename from target path as layer name
    target_layer_name = os.path.splitext(
        os.path.basename(target_merged_vector_path))[0]

    # Create target layer using same projection from base vector
    target_layer = target_vector.CreateLayer(
        target_layer_name,
        base_layer.GetSpatialRef(), ogr.wkbPolygon)

    # Write the merged geometry to the target layer
    target_layer.StartTransaction()
    target_feature = ogr.Feature(target_layer.GetLayerDefn())

    # Make a geometry, from Shapely object
    target_feature.SetGeometry(ogr.CreateGeometryFromWkb(merged_geom.wkb))
    target_layer.CreateFeature(target_feature)
    target_layer.CommitTransaction()

    base_layer = None
    base_vector = None
    target_layer = None
    target_vector = None


def _has_field_name(base_vector_path, field_name):
    """Check if the vector attribute table has the designated field name.

    Parameters:
        base_vector_path (str): a path to the vector to check the field name
            with.

        field_name (str): the field name to be inspected.

    Returns:
        True if the field name exists, False if it doesn't.

    Raises:
        ValueError if two or more field names show up in the attributes.

    """
    base_vector = gdal.OpenEx(base_vector_path, gdal.OF_VECTOR)
    base_layer = base_vector.GetLayer()
    base_layer_defn = base_layer.GetLayerDefn()
    field_count = base_layer_defn.GetFieldCount()
    field_name_counts = 0

    for fld_index in range(field_count):
        field = base_layer_defn.GetFieldDefn(fld_index)
        base_field_name = field.GetName()
        if base_field_name == field_name:
            field_name_counts += 1
    base_vector = None
    base_layer = None

    if field_name_counts == 0:
        LOGGER.info('The %s field is not provided in AOI vector.' % field_name)
        return False

    elif field_name_counts == 1:
        LOGGER.info('The %s field is provided in AOI vector.' % field_name)
        return True


def _ecosystem_risk_op(ecosystem_arr, max_risk_score, *hab_risk_arrays):
    """Calculate cumulative habitat risk scores from hab_risk_arrays.

    First divide the total risk by the number of habitats on each pixel.
    Then if 0 < 3*(risk/max risk) <= 1, classify the risk score to 1.
    If 1 < 3*(risk/max risk) <= 2, classify the risk score to 2.
    If 2 < 3*(risk/max risk) <= 3 , classify the risk score to 3.
    Note: If 3*(risk/max risk) == 0, it will remain 0, meaning that there's no
    stressor on the ecosystem.

    Parameters:
        ecosys_arr (array): an array with each pixel indicating the number of
            habitats existing on that pixel.

        max_risk_score (float): the maximum possible risk score used for
            reclassifying the risk score on each pixel.

        *hab_risk_arrays: a list of arrays representing reclassified
            risk scores for each habitat.

    """
    ecosystem_risk_arr = numpy.full(
        ecosystem_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    ecosystem_mask = (ecosystem_arr > 0) & (ecosystem_arr != _TARGET_NODATA_INT)
    ecosystem_risk_arr[ecosystem_mask] = 0

    # Add up all the risks of each habitat
    for hab_risk_arr in hab_risk_arrays:
        valid_risk_mask = (hab_risk_arr != _TARGET_NODATA_FLT)
        ecosystem_risk_arr[valid_risk_mask] += hab_risk_arr[valid_risk_mask]

    # Divide risk score by the number of habitats in each pixel. This way we
    # could normalize the risk and not be biased by any large risk score
    # resulting from the existence of multiple habitats
    ecosystem_risk_arr[ecosystem_mask] /= ecosystem_arr[ecosystem_mask]

    # Divide risk score by (maximum possible risk score/3) to get a value
    # ranging from 0 to 3, then return the ceiling of the output
    ecosystem_risk_arr[ecosystem_mask] = numpy.ceil(
        ecosystem_risk_arr[ecosystem_mask] / (max_risk_score/3.))

    return ecosystem_risk_arr


def _reclassify_risk_op(risk_arr, max_risk_score):
    """Reclassify total risk score on each pixel into 1 to 3.

    If 0 < 3*(risk/max risk) <= 1, classify the risk score to 1.
    If 1 < 3*(risk/max risk) <= 2, classify the risk score to 2.
    If 2 < 3*(risk/max risk) <= 3 , classify the risk score to 3.
    Note: If 3*(risk/max risk) == 0, it will remain 0, meaning that there's no
    stressor on that habitit.

    Parameters:
        risk_arr (array): an array of cumulative risk scores from all stressors

        max_risk_score (float): the maximum possible risk score used for
            reclassifying the risk score on each pixel.

    Returns:
        reclass_arr (array): an array of reclassified risk scores for a
            certain habitat.

    """
    reclass_arr = numpy.full(
        risk_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    valid_pixel_mask = (risk_arr != _TARGET_NODATA_FLT)

    # Divide risk score by (maximum possible risk score/3) to get a value
    # ranging from 0 to 3, then return the ceiling of the output
    reclass_arr[valid_pixel_mask] = numpy.ceil(risk_arr[valid_pixel_mask] / (
        max_risk_score/3.))

    return reclass_arr


def _ecosystem_op(*habitat_arrays):
    """Adding pixel values together from multiple arrays.

    Parameters:
        *habitat_arrays: a list of arrays with 1s and 0s values.

    Returns:
        ecosys_arr (array): an array with each pixel indicating the summation
            value of input habitat arrays.

    """
    # Since the habitat arrays have been aligned, we can just use the shape
    # of the first habitat array
    ecosys_arr = numpy.full(
        habitat_arrays[0].shape, 0, dtype=numpy.int8)

    for habitat_arr in habitat_arrays:
        habiat_mask = (habitat_arr != _TARGET_NODATA_INT)
        ecosys_arr[habiat_mask] += habitat_arr.astype(int)[habiat_mask]

    return ecosys_arr


def _stressor_overlap_op(ecosystem_arr, *stressor_arrays):
    """Adding pixel values together from multiple arrays.

    Parameters:
        ecosys_arr (array): an array with each pixel indicating the summation
            value of input habitat arrays.

        *stressor_arrays: a list of arrays with 1s and 0s values.

    Returns:
        overlap_arr (array): an array with each pixel indicating the summation
            value of input arrays.

    """
    overlap_arr = numpy.full(
        ecosystem_arr.shape, 0, dtype=numpy.int8)

    # Sum up the pixel values of each stressor array
    for stressor_arr in stressor_arrays:
        valid_pixel_mask = (stressor_arr != _TARGET_NODATA_INT)
        overlap_arr[valid_pixel_mask] += stressor_arr.astype(int)[
            valid_pixel_mask]

    # Convert the values outside of the ecosystem to nodata, since they won't
    # affect the risk of any habitats in the ecosystem
    non_ecosystem_mask = (ecosystem_arr < 1) | (
        ecosystem_arr == _TARGET_NODATA_INT)
    overlap_arr[non_ecosystem_mask] = _TARGET_NODATA_INT

    return overlap_arr


def _get_max_risk_score(
        stressor_path_list, habitat_path_list, target_overlap_stressor_path,
        target_ecosystem_raster_path, max_rating, risk_eq):
    """Calculate the maximum risk score based on stressor number and ratings.

    The maximum possible risk score is calculatd by either multiplying the
    number of stressors and maximum rating (Multiplicative), or multiplying the
    number of stressors and the euclidean distance of the maximum ratings
    (Euclidean).

    Parameters:
        stressor_path_list (list): a list of stressor raster paths with pixel
            values of 1 representing stressor existence and 0 non-existence.

        habitat_path_list (list): a list of habitat raster paths with pixel
            values of 1 representing stressor existence and 0 non-existence.

        target_overlap_stressor_path (str): a path to the output raster that
            has number of overlapping stressors on each pixel.

        target_ecosystem_raster_path (str): a path to the output raster that
            has 1s indicating habitat existence and 0s non-existence.

        max_rating (float): a number representing the highest potential value
            that should be represented in rating in the criteria CSV table.

        risk_eq (str): a string identifying the equation that should be
            used in calculating risk scores for each H-S overlap cell. This
            will be either 'Euclidean' or 'Multiplicative'.

    Returns:
        max_risk_score (float): the maximum possible risk score that is likely
            occur to a single habitat pixel.

    """
    habitat_path_band_list = [(path, 1) for path in habitat_path_list]
    pygeoprocessing.raster_calculator(
        habitat_path_band_list, _ecosystem_op,
        target_ecosystem_raster_path, gdal.GDT_Byte, _TARGET_NODATA_INT)

    stressor_path_band_list = [(path, 1) for path in stressor_path_list]
    stressor_path_band_list.insert(0, (target_ecosystem_raster_path, 1))
    pygeoprocessing.raster_calculator(
        stressor_path_band_list, _stressor_overlap_op,
        target_overlap_stressor_path, gdal.GDT_Byte, _TARGET_NODATA_INT)

    # Get maximum overlapping stressors from the output band statistics
    raster = gdal.OpenEx(target_overlap_stressor_path, gdal.OF_RASTER)
    band = raster.GetRasterBand(1)
    max_overlap_stressors = band.GetMaximum()
    raster = None

    # Calculate the maximum risk score for a habitat from all stressors
    if risk_eq == 'Multiplicative':
        # The maximum score for a single stressor is max_rating*max_rating
        max_risk_score = max_overlap_stressors*(max_rating*max_rating)
    else:  # risk_eq is 'Euclidean'
        # The maximum risk score for a habitat from a single stressor is
        # sqrt( (max_rating-1)^2 + (max_rating-1)^2 ). Therefore multiply that
        # by the number of stressors to get maximum possible risk scores.
        max_risk_score = max_overlap_stressors*numpy.sqrt(
            numpy.power((max_rating-1), 2)*2)

    LOGGER.debug('max_overlap_stressors: %s. max_risk_score: %s.' %
                 (max_overlap_stressors, max_risk_score))

    return max_risk_score


def _tot_risk_op(habitat_arr, *indi_risk_arrays):
    """Calculate cumulative risk to a habitat or species from all stressors.

    The risk score is calculated by summing up all the risk scores on each
    valid pixel of the habitat.

    Parameters:
        habitat_arr (array): a habitat array where 1's indicates habitat
            existence and 0's non-existence.

        *indi_risk_arrays: a list of individual risk arrays from each stressor
            to a certain habitat.

    Returns:
        tot_risk_arr (array): a cumulative risk array calculated by summing
            all the individual risk arrays.

    """
    # Fill 0s to the total risk array on where habitat exists
    habitat_mask = (habitat_arr == 1)
    tot_risk_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    tot_risk_arr[habitat_mask] = 0

    for indi_risk_arr in indi_risk_arrays:
        valid_pixel_mask = (indi_risk_arr != _TARGET_NODATA_FLT)
        tot_risk_arr[valid_pixel_mask] += indi_risk_arr[valid_pixel_mask]

    return tot_risk_arr


def _pair_risk_op(exposure_arr, consequence_arr, risk_eq):
    """Calculate habitat-stressor risk array based on the risk equation.

    Euclidean risk equation: R = sqrt((E-1)^2 + (C-1)^2)
    Multiplicative risk equation: R = E * C

    Parameters:
        exosure_arr (array): an array with total exposure scores.

        consequence_arr (array): an array with total consequence scores.

        risk_eq (str): a string identifying the equation that should be
            used in calculating risk scores. It could be either 'Euclidean' or
            'Multiplicative'.

    Returns:
        risk_arr (array): a risk array calculated based on the risk equation.

    """
    risk_arr = numpy.full(
        exposure_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    zero_pixel_mask = (exposure_arr == 0) | (consequence_arr == 0)
    valid_pixel_mask = (exposure_arr != _TARGET_NODATA_FLT) & (
        consequence_arr != _TARGET_NODATA_FLT)
    nonzero_valid_pixel_mask = ~zero_pixel_mask & valid_pixel_mask

    # Zero pixels are where non of the stressor exists in the habitat
    risk_arr[zero_pixel_mask] = 0

    if risk_eq == 'Euclidean':
        # If E-1 or C-1 is less than 0, replace the pixel value with 0
        risk_arr[nonzero_valid_pixel_mask] = numpy.sqrt(
            numpy.power(
                numpy.maximum(
                    exposure_arr[nonzero_valid_pixel_mask]-1, 0), 2) +
            numpy.power(
                numpy.maximum(
                    consequence_arr[nonzero_valid_pixel_mask]-1, 0), 2))

    else:  # Multiplicative
        risk_arr[nonzero_valid_pixel_mask] = numpy.multiply(
            exposure_arr[nonzero_valid_pixel_mask],
            consequence_arr[nonzero_valid_pixel_mask])

    return risk_arr


def _final_expo_score_op(habitat_arr, *num_denom_list):
    """Calculate the exposure score for a habitat layer from all stressors.

    Add up all the numerators and denominators respectively, then divide
    the total numerator by the total denominator on habitat pixels, to get
    the final exposure or consequence score.

    Parameters:
        habitat_arr (array): a habitat array where 1's indicates habitat
            existence and 0's non-existence.

        *num_denom_list (list): if exists, it's a list of numerator arrays in
            the first half of the list, and denominator scores the second half.

    Returns:
        final_expo_arr (array): an exposure array calculated by dividing the
            total numerator by the total denominator. Pixel values are nodata
            outside of habitat, and will be 0 if there is no valid numerator
            value on that pixel.

    """
    habitat_mask = (habitat_arr == 1)

    # Fill each array with value of 0 on the habitat pixels, assuming that
    # the risk score on that habitat is 0 before adding numerator/denominator
    tot_num_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    tot_num_arr[habitat_mask] = 0

    final_expo_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    final_expo_arr[habitat_mask] = 0

    tot_denom = 0

    # Numerator arrays are in the first half of the list
    num_arr_list = num_denom_list[:len(num_denom_list)/2]
    denom_list = num_denom_list[len(num_denom_list)/2:]

    # Calculate the cumulative numerator and denominator values
    for num_arr in num_arr_list:
        valid_num_mask = (num_arr != _TARGET_NODATA_FLT)
        tot_num_arr[valid_num_mask] += num_arr[valid_num_mask]

    for denom in denom_list:
        tot_denom += denom

    # If the numerator is nodata, do not divide the arrays
    final_valid_mask = (tot_num_arr != _TARGET_NODATA_FLT)

    final_expo_arr[final_valid_mask] = tot_num_arr[
        final_valid_mask] / tot_denom

    return final_expo_arr


def _final_conseq_score_op(habitat_arr, recov_num_arr, *num_denom_list):
    """Calculate the consequence score for a habitat layer from all stressors.

    Add up all the numerators and denominators respectively, then divide
    the total numerator by the total denominator on habitat pixels, to get
    the final exposure or consequence score.

    Parameters:
        habitat_arr (array): a habitat array where 1's indicates habitat
            existence and 0's non-existence.

        recov_num_arr (array): an array of the numerator score from recovery
            potential, to be added to the consequence numerator scores.

        *num_denom_list (list): if exists, it's a list of numerator arrays in
            the first half of the list, and denominator scores occupies the
            second half in addition to the last denominator score from recovery
            potential.

    Returns:
        final_conseq_arr (array): a consequence array calculated by dividing
            the total numerator by the total denominator. Pixel values are
            nodata outside of habitat, and will be 0 if there is no valid
            numerator value on that pixel.

    """
    habitat_mask = (habitat_arr == 1)

    tot_num_arr = numpy.copy(recov_num_arr)

    # Fill each array with value of 0 on the habitat pixels, assuming that
    # criteria score is 0 before adding numerator/denominator
    final_conseq_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    final_conseq_arr[habitat_mask] = 0

    tot_denom = 0

    # Numerator arrays are in the first half of the list
    num_arr_list = num_denom_list[:(len(num_denom_list)-1)/2]
    denom_list = num_denom_list[(len(num_denom_list)-1)/2:]

    # Calculate the cumulative numerator and denominator values
    for num_arr in num_arr_list:
        valid_num_mask = (num_arr != _TARGET_NODATA_FLT)
        tot_num_arr[valid_num_mask] += num_arr[valid_num_mask]

    for denom in denom_list:
        tot_denom += denom

    # If the numerator is nodata, do not divide the arrays
    final_valid_mask = (tot_num_arr != _TARGET_NODATA_FLT)

    final_conseq_arr[final_valid_mask] = tot_num_arr[
        final_valid_mask] / tot_denom

    return final_conseq_arr


def _pair_criteria_score_op(
        habitat_arr, stressor_dist_arr, stressor_buffer, num_arr, denom):
    """Calculate individual E/C scores by dividing num by denom arrays.

    The equation for calculating the score is numerator/denominator. This
    function will only calculate the score on pixels where both habitat
    and stressor (including buffer zone) exist.

    Parameters:
        habitat_arr (array): a habitat array where 1's indicates habitat
            existence and 0's non-existence.

        stressor_dist_arr (array): a stressor distance array where pixel
            values represent the distance of that pixel to a stressor
            pixel.

        stressor_buffor (float): a number representing how far down the
            influence is from the stressor pixel.

        num_arr (array): an array of the numerator scores calculated based on
            the E/C equation.

        denom (float): a cumulative value pre-calculated based on the criteria
            CSV table. It will be used to divide the numerator.

    Returns:
        score_arr (array): an array of the scores calculated based on
            the E/C equation in users guide.

    """
    habitat_mask = (habitat_arr == 1)
    stressor_mask = (stressor_dist_arr == 0)
    # Habitat-stressor overlap mask that excludes stressor buffer
    hab_stress_overlap_mask = (habitat_mask & stressor_mask)

    # Mask stressor buffer zone
    stressor_buff_mask = (
        (stressor_dist_arr > 0) & (stressor_dist_arr < stressor_buffer))
    hab_buff_overlap_mask = (habitat_mask & stressor_buff_mask)

    # Denominator would always be unaffected by ratings in the area where
    # habitat and stressor + stressor buffer overlap
    hab_stress_buff_mask = (hab_stress_overlap_mask |
                            hab_buff_overlap_mask)

    # Initialize output exposure or consequence score array
    score_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)

    score_arr[hab_stress_buff_mask] = num_arr[hab_stress_buff_mask] / denom

    return score_arr


def _pair_criteria_num_op(
        habitat_arr, stressor_dist_arr, stressor_buffer, decay_eq, num,
        *spatial_explicit_arr_const):
    """Calculate E or C numerator scores with distance decay equation.

    The equation for calculating the numerator is rating/(dq*weight). This
    function will only calculate the score on pixels where both habitat
    and stressor (including buffer zone) exist. A spatial criteria will be
    added if spatial_explicit_arr_const is provided.

    Parameters:
        habitat_arr (array): a habitat array where 1s indicates habitat
            existence and 0s non-existence.

        stressor_dist_arr (array): a stressor distance array where pixel
            values represent the distance of that pixel to a stressor
            pixel.

        stressor_buffer (float): a number representing how far down the
            influence is from the stressor pixel.

        decay_eq (str): a string representing the decay format of the
            stressor in the buffer zone. Could be `None`, `Linear`, or
            `Exponential`.

        num (float): a cumulative value pre-calculated based on the criteria
            CSV table. It will be divided by denominator to get exposure score.

        *spatial_explicit_arr_const: if exists, it is a list of variables
            representing stressor attribute array, DQ, weight, and nodata
            respectively on every four items.

    Returns:
        num_arr (array): an array of the numerator scores calculated based on
            the E/C equation.

    """
    habitat_mask = (habitat_arr == 1)
    stressor_buff_mask = (stressor_dist_arr <= stressor_buffer)
    # Habitat-stressor overlap mask that includes stressor buffer
    hab_stress_buff_mask = (habitat_mask & stressor_buff_mask)

    # Initialize numerator and denominator arrays and fill the habitat-
    # stressor overlapping pixels with corresponding values
    num_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    num_arr[hab_stress_buff_mask] = num

    # Loop through every 4 items in spatial_explicit_arr_const, and compute
    # the cumulative numerator values over the array
    for spatial_arr, dq, weight, nodata in zip(
            spatial_explicit_arr_const[0::4],
            spatial_explicit_arr_const[1::4],
            spatial_explicit_arr_const[2::4],
            spatial_explicit_arr_const[3::4]):
        # Mask pixels where both habitat, stressor, and spatial array exist
        overlap_mask = [hab_stress_buff_mask & (spatial_arr != nodata)]

        # Compute the cumulative numerator score
        num_arr[overlap_mask] += spatial_arr[overlap_mask]/(dq*weight)

    # Mask habitat-stressor buffer zone, excluding the stressor itself
    hab_buff_mask = (hab_stress_buff_mask & (stressor_dist_arr > 0))

    # Both linear and exponential decay equations assumed that numerator
    # value is zero outside of buffer zone, and decays over distance
    # in the overlapping area of habitat and buffer zone
    if decay_eq == 'Linear':
        # Linearly decays over distance
        num_arr[hab_buff_mask] = num_arr[hab_buff_mask] * (
            1. - stressor_dist_arr[hab_buff_mask] /
            stressor_buffer)

    elif decay_eq == 'Exponential':
        # This decay rate makes the target numerator zero outside of the
        # stressor buffer
        decay_rate = numpy.log(
            _EXP_DEDAY_CUTOFF/num_arr[hab_buff_mask]) / stressor_buffer
        # Only calculate the decaying numerator score within the buffer zone
        num_arr[hab_buff_mask] = num_arr[hab_buff_mask] * numpy.exp(
            decay_rate *
            stressor_dist_arr[hab_buff_mask])

    return num_arr


def _calc_pair_criteria_score(
        habitat_stressor_overlap_df, habitat_raster_path,
        stressor_dist_raster_path, stressor_buffer, decay_eq,
        criteria_type, graph, dependent_task_list):
    """Calculate exposure or consequence scores for a habitat-stressor pair.

    Parameters:
        habitat_stressor_overlap_df (dataframe): a dataframe that has
            information on stressor and habitat overlap property.

        habitat_raster_path (str): a path to the habitat raster where 0's
            indicate no habitat and 1's indicate habitat existence. 1's will be
            used for calculating recovery potential output raster.

        stressor_dist_raster_path (str): a path to a raster where each pixel
            represents the Euclidean distance to the closest stressor pixel.

        stressor_buffor (float): a number representing how far down the
            influence is from the stressor pixel.

        decay_eq (str): a string representing the decay format of the
            stressor in the buffer zone. Could be `None`, `Linear`, or
            `Exponential`.

        criteria_type (str): a string indicating that this function calculates
            exposure or consequence scores. Could be `C` or `E`. If `C`,
            recov_score_paths needs to be added.

        graph (Taskgraph object): an object for building task graphs and
            parallelizing independent tasks.

        dependent_task_list (list): a list of tasks that the tasks for
            calculating numerators and criteria scores will be dependent upon.

        recov_num_path (str): a path to the recovery numerator raster
            calculated based on habitat resilience attribute. The array values
            will be added to consequence scores.

    Returns:
        None.

    """
    header_list = ['NUM', 'DENOM', 'SPATIAL', 'NUM_RASTER_PATH',
                   'RASTER_PATH']
    header_list = [criteria_type + '_' + header for header in header_list]

    num, denom, spatial_explicit_dict, target_criteria_num_path, \
        target_pair_criteria_raster_path = [
            habitat_stressor_overlap_df.loc[header] for header in header_list]

    # A path and/or constant list for calculating numerator rasters
    num_list = [
        (habitat_raster_path, 1), (stressor_dist_raster_path, 1),
        (stressor_buffer, 'raw'), (decay_eq, 'raw'), (num, 'raw')]

    # A path and/or constant list for calculating final E or C score
    final_score_list = [
        (habitat_raster_path, 1), (stressor_dist_raster_path, 1),
        (stressor_buffer, 'raw'), (target_criteria_num_path, 1),
        (denom, 'raw')]

    # Iterate through each stressor overlap attribute and append spatial
    # explicit path, DQ, and weight to the path band constant list
    for stressor_attribute_key in spatial_explicit_dict:
        attr_raster_path, dq, weight = spatial_explicit_dict[
            stressor_attribute_key]
        attr_nodata = pygeoprocessing.get_raster_info(attr_raster_path)[
            'nodata'][0]
        num_list.append((attr_raster_path, 1))
        num_list.append((float(dq), 'raw'))
        num_list.append((float(weight), 'raw'))
        num_list.append((attr_nodata, 'raw'))

    task_name = 'exposure' if criteria_type == 'E' else 'consequence'

    # Calculate numerator raster for the habitat-stressor pair
    calc_criteria_num_task = graph.add_task(
        func=pygeoprocessing.raster_calculator,
        args=(num_list, _pair_criteria_num_op, target_criteria_num_path,
              _TARGET_FLT_PIXEL, _TARGET_NODATA_FLT),
        target_path_list=[target_criteria_num_path],
        task_name='calculate_%s_num_scores' % task_name,
        dependent_task_list=dependent_task_list)
    dependent_task_list.append(calc_criteria_num_task)

    # Calculate E or C raster for the habitat-stressor pair. This task is
    # dependent upon the numerator calculation task
    calc_criteria_score_task = graph.add_task(
        func=pygeoprocessing.raster_calculator,
        args=(final_score_list, _pair_criteria_score_op,
              target_pair_criteria_raster_path, _TARGET_FLT_PIXEL,
              _TARGET_NODATA_FLT),
        target_path_list=[target_pair_criteria_raster_path],
        task_name='calculate_%s_scores' % task_name,
        dependent_task_list=dependent_task_list)
    dependent_task_list.append(calc_criteria_score_task)


def _final_recovery_op(habitat_arr, num_arr, denom, max_rating):
    """Calculate and reclassify habitat recovery scores to 1 to 3.

    The equation for calculating reclassified recovery score is:
        score = 3 * (1 - num/denom/max_rating)
    If 0 < score <= 1, reclassify it to 1.
    If 1 < score <= 2, reclassify it to 2.
    If 2 < score <= 3, reclassify it to 3.

    Parameters:
        habitat_arr (array): a habitat array where 1's indicates habitat
            existence and 0's non-existence.

        num_arr (array): an array of the numerator score for recovery
            potential.

        denom (float): the precalculated cumulative denominator score.

        max_rating (float): the rating used to define the recovery reclassified.

    Returns:
        output_recovery_arr (array): an array of the recovery potential
            score calculated based on the recovery potential equation.

    """
    # Initialize numerator and denominator arrays and fill the habitat
    # pixels with corresponding values
    habitat_mask = (habitat_arr == 1)

    recov_reclass_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)

    # Calculate the recovery score by dividing numerator by denominator
    # and then convert it to reclassified by using max_rating
    recov_reclass_arr[habitat_mask] = numpy.ceil(
        3. - num_arr[habitat_mask] / denom / max_rating * 3.)

    return recov_reclass_arr


def _recovery_num_op(habitat_arr, num, *spatial_explicit_arr_const):
    """Calculate the numerator score for recovery potential on a habitat array.

    The equation for calculating the numerator score is rating/(dq*weight).
    This function will only calculate the score on pixels where habitat exists,
    and use a spatial criteria if spatial_explicit_arr_const is provided.

    Parameters:
        habitat_arr (array): a habitat array where 1's indicates habitat
            existence and 0's non-existence.

        num (float): a cumulative value pre-calculated based on the criteria
            CSV table. It will be divided by denominator to get exposure score.

        *spatial_explicit_arr_const: if exists, it is a list of variables
            representing resilience array, DQ, weight, and nodata
            respectively on every four items.

    Returns:
        num_arr (array): an array of the numerator score for recovery potential.

    """
    # Initialize numerator and denominator arrays and fill the habitat
    # pixels with corresponding values
    habitat_mask = (habitat_arr == 1)

    num_arr = numpy.full(
        habitat_arr.shape, _TARGET_NODATA_FLT, dtype=numpy.float32)
    num_arr[habitat_mask] = num

    # Loop through every 4 items in spatial_explicit_arr_const, and compute the
    # numerator values cumulatively
    for resilience_arr, dq, weight, nodata in zip(
            spatial_explicit_arr_const[0::4],
            spatial_explicit_arr_const[1::4],
            spatial_explicit_arr_const[2::4],
            spatial_explicit_arr_const[3::4]):
        # Mask pixels where both habitat and resilience score exist
        hab_res_overlap_mask = [
           habitat_mask & (resilience_arr != nodata)]

        # Compute cumulative numerator score
        num_arr[hab_res_overlap_mask] += resilience_arr[
            hab_res_overlap_mask]/(dq*weight)

    return num_arr


def _calc_habitat_recovery(
        habitat_raster_path, habitat_name, recovery_df, max_rating):
    """Calculate habitat raster recovery potential based on recovery scores.

    Parameters:
        habitat_raster_path (str): a path to the habitat raster where 0's
            indicate no habitat and 1's indicate habitat existence. 1's will be
            used for calculating recovery potential output raster.

        habitat_name (str): the habitat name for finding the information on
            recovery potential from recovery_df.

        recovery_df (dataframe): the dataframe with recovery information such
            as numerator and denominator scores, spatially explicit criteria
            dictionary, and target habitat recovery raster paths.

        max_rating (float): the rating used to reclassify the recovery score.

    Returns:
        None

    """
    # Get a list of cumulative numerator and denominator scores, spatial
    # explicit dict which has habitat-resilience as key and resilience raster
    # path, DQ and weight as values, and an output file paths
    num, denom, spatial_explicit_dict, target_r_num_raster_path, \
        target_recov_raster_path = [
            recovery_df.loc[habitat_name, column_header] for column_header in [
                'R_NUM', 'R_DENOM', 'R_SPATIAL', 'R_NUM_RASTER_PATH',
                'R_RASTER_PATH']]

    # A list for calculating arrays of cumulative numerator scores
    num_list = [(habitat_raster_path, 1), (num, 'raw')]

    # A list for calculating recovery potential
    recov_potential_list = [
        (habitat_raster_path, 1), (target_r_num_raster_path, 1),
        (denom, 'raw'), (max_rating, 'raw')]

    # Iterate through the spatially explicit criteria dictionary and append its
    # raster path, DQ, and weight to num_list
    for habitat_resilience_key in spatial_explicit_dict:
        resilinece_raster_path, dq, weight = spatial_explicit_dict[
            habitat_resilience_key]
        resilience_nodata = pygeoprocessing.get_raster_info(
            resilinece_raster_path)['nodata'][0]
        num_list.append((resilinece_raster_path, 1))
        num_list.append((float(dq), 'raw'))
        num_list.append((float(weight), 'raw'))
        num_list.append((resilience_nodata, 'raw'))

    # Calculate cumulative numerator score for the habitat
    pygeoprocessing.raster_calculator(
        num_list, _recovery_num_op, target_r_num_raster_path,
        _TARGET_FLT_PIXEL, _TARGET_NODATA_FLT)

    # Finally calculate recovery potential for the habitat
    pygeoprocessing.raster_calculator(
        recov_potential_list, _final_recovery_op, target_recov_raster_path,
        _TARGET_FLT_PIXEL, _TARGET_NODATA_FLT)


def _append_spatial_raster_row(info_df, recovery_df, overlap_df,
                               spatial_file_dir, output_dir, suffix_end):
    """Append spatial raster to NAME, PATH, and TYPE column of info_df.

    Parameters:
        info_df (dataframe): the dataframe to append spatial raster information
            to.

        recovery_df (dataframe): the dataframe that has the spatial raster
            information on its `R_SPATIAL` column.

        overlap_df (dataframe): the multi-index dataframe that has the spatial
            raster information on its `E_SPATIAL` and `C_SPATIAL` columns.

        spatial_file_dir (str): the path to the root directory where the
            absolute paths of spatial files will be created based on.

        output_dir (str): a path to the folder for creating new raster paths at

        suffix_end (str): a suffix to be appended a the end of the filenames.

    Returns:
        info_df (dataframe): a dataframe appended with spatial raster info.

    """
    raster_dicts_list = recovery_df['R_SPATIAL'].tolist() + overlap_df[
        'E_SPATIAL'].tolist() + overlap_df['C_SPATIAL'].tolist()
    # Starting index would be the last index in info_df + 1
    start_idx = info_df.index.values[-1] + 1

    for raster_dict in raster_dicts_list:
        for raster_name in raster_dict:
            # The first item in the list of that raster_name key would be the
            # path to that raster
            raster_path = raster_dict[raster_name][0]
            info_df.loc[start_idx, 'NAME'] = raster_name
            info_df.loc[start_idx, 'PATH'] = raster_path
            info_df.loc[start_idx, 'TYPE'] = _SPATIAL_CRITERIA_TYPE

            # Convert all relative paths to absolute paths
            info_df['PATH'] = info_df.apply(
                lambda row: _to_abspath(
                    row['PATH'], spatial_file_dir), axis=1)
            # Check if the file on the path is a raster or vector
            info_df['IS_RASTER'] = info_df.apply(
                lambda row: _label_raster(row['PATH']), axis=1)
            # Generate simplified vector path if the file is a vector
            info_df['SIMPLE_VECTOR_PATH'] = info_df.apply(
                lambda row: _generate_vector_path(
                    row, output_dir, 'simplified_', suffix_end), axis=1)
            # Generate raster paths which vectors will be rasterized onto
            info_df['BASE_RASTER_PATH'] = info_df.apply(
                lambda row: _generate_raster_path(
                    row, output_dir, 'base_', suffix_end), axis=1)
            # Generate raster paths which will be aligned & resized from base
            # rasters
            info_df['ALIGN_RASTER_PATH'] = info_df.apply(
                lambda row: _generate_raster_path(
                    row, output_dir, 'aligned_', suffix_end), axis=1)
            # Get raster linear unit, and raise an exception if the projection
            # is missing
            info_df['LINEAR_UNIT'] = info_df.apply(
                lambda row: _label_linear_unit(row), axis=1)

            # Replace the raster path in that dict with the new aligned
            # raster path
            raster_dict[raster_name][0] = info_df.loc[
                start_idx, 'ALIGN_RASTER_PATH']

            start_idx += 1

    return info_df


def _to_abspath(path, dir_path):
    """Return an absolute path within dir_path if the given path is relative.

    Parameters:
        path (str): a path to the file to be examined.

        dir_path (str): a path to the directory which will be used to create
            absolute file paths.

    Returns:
        path (str): an absolutized version of the path.

    Raises:
        ValueError if the file doesn't exist.

    """
    if not os.path.isabs(path):
        path = os.path.join(dir_path, path)

    if not os.path.exists(path):
        raise ValueError('The file on %s does not exist.' % path)

    return path


def _label_raster(path):
    """Open a file given the path, and label whether it's a raster.

    If the provided path is a relative path, join it with the dir_path provide.

    Parameters:
        path (str): a path to the file to be opened with GDAL.

    Returns:
        A string of either 'true', 'false', or 'invalid', indicating the
        file path has a raster, vector, or invalid file type.

    Raises:
        ValueError if the file can't be opened by GDAL.

    """
    raster = gdal.OpenEx(path, gdal.OF_RASTER)
    if raster:
        raster = None
        return True
    else:
        vector = gdal.OpenEx(path, gdal.OF_VECTOR)
        if vector:
            vector = None
            return False
        else:
            raise ValueError(
                'The file on %s is a not a valid GDAL file.' % path)


def _generate_raster_path(row, dir_path, suffix_front, suffix_end):
    """Generate a raster file path with suffixes to the output folder.

    Also append suffix to the raster file name.

    Parameters:
        row (pandas.Series): a row on the dataframe to get path value from.

        dir_path (str): a path to the folder which raster paths will be
            created based on.

        suffix (str): a suffix appended to the end of the raster file name.

        suffix_end (str): a file suffix to append to the end of filenames.

    Returns:
        Original path if path is already a raster, or target_raster_path within
        dir_path if it's a vector.

    """
    path = row['PATH']
    # Get file base name without extension
    basename = os.path.splitext(os.path.basename(path))[0]
    target_raster_path = os.path.join(
        dir_path,
        suffix_front + basename + suffix_end + '.tif')

    # Return the original file path from `PATH` if it's already a raster
    if suffix_front == 'base_' and row['IS_RASTER']:
        return path
    # Habitat rasters do not need to be transformed
    elif (suffix_front == 'dist_' or suffix_front == 'buff_') and (
          row['TYPE'] == 'habitat'):
        return None
    else:
        return target_raster_path


def _generate_vector_path(row, dir_path, suffix_front, suffix_end):
    """Generate a vector file path with suffixes to the output folder.

    Also append suffix to the raster file name.

    Parameters:
        row (pandas.Series): a row on the dataframe to get path value from.

        dir_path (str): a path to the folder which raster paths will be
            created based on.

        suffix (str): a suffix appended to the end of the raster file name.

        suffix_end (str): a file suffix to append to the end of filenames.

    Returns:
        Original path if path is already a raster, or a raster path within
        dir_path if it's a vector.

    """
    if not row['IS_RASTER']:
        # Generate a new vector path with suffix in the dir_path
        path = row['PATH']
        # Get file base name without extension
        basename = os.path.splitext(os.path.basename(path))[0]
        target_vector_path = os.path.join(
            dir_path,
            suffix_front + basename + suffix_end + '.gpkg')
        return target_vector_path
    else:
        return None


def _label_linear_unit(row):
    """Get linear unit from path, and keep track of paths w/o projection.

    Parameters:
        row (pandas.Series): a row on the dataframe to get path value from.

    Returns:
        linear_unit (float): the value to multiply by linear distances in order
            to transform them to meters

    Raises:
        ValueError if any of the file projections is missing.

    """
    if row['IS_RASTER']:
        sr_wkt = pygeoprocessing.get_raster_info(row['PATH'])['projection']
    else:
        sr_wkt = pygeoprocessing.get_vector_info(row['PATH'])['projection']

    if not sr_wkt:
        raise ValueError('The following layer does not have a projection: %s' %
                         row['PATH'])
    else:
        sr = osr.SpatialReference()
        sr.ImportFromWkt(sr_wkt)
        linear_unit = sr.GetLinearUnits()
        return linear_unit


def _get_info_dataframe(base_info_csv_path, file_preprocessing_dir,
                        intermediate_dir, output_dir, suffix_end):
    """Read CSV file as dataframe and add data info to new columns.

    Add new columns that provide file information and target file paths of each
    given habitat or stressors to the dataframe.

    Parameters:
        base_info_csv_path (str): a path to the CSV file that contains the path
            and buffer information.

        file_preprocessing_dir (str): a path to the folder where simplified
            vectors paths, and base, aligned and distance raster paths will
            be created in.

        intermediate_dir (str): a path to the folder where cumulative
            exposure and consequence raster paths for each habitat will be
            created in.

        output_dir (str): a path to the folder where risk raster path for each
            habitat will be created in.

        suffix_end (str): a file suffix to append to the end of filenames.

    Returns:
        info_df (dataframe): a dataframe that has the information on whether a
            file is a vector, and a raster path column.

        habitat_names (list): a list of habitat names obtained from info CSV.

        stressor_names (list): a list of stressor names obtained from info CSV.

    Raises:
        ValueError if any column header is missing from the CSV file.
        ValueError if any input format is not correct.
        ValueError if if any input file does not have a projection.

    """
    required_column_headers = ['NAME', 'PATH', 'TYPE', _BUFFER_HEADER]
    required_types = ['habitat', 'stressor']
    required_buffer_type = 'stressor'

    info_df = pandas.read_csv(base_info_csv_path)
    info_df.columns = map(str.upper, info_df.columns)
    missing_columns = list(
        set(required_column_headers) - set(info_df.columns.values))

    if missing_columns:
        raise ValueError(
            'Missing column header(s) from the Info CSV file: %s' %
            missing_columns)

    # Convert the values in TYPE column to lowercase first
    info_df.TYPE = info_df.TYPE.str.lower()
    unknown_types = list(set(info_df.TYPE) - set(required_types))
    if unknown_types:
        raise ValueError(
            'The `TYPE` attribute in Info CSV could only have either `habitat`'
            ' or `stressor` as its value, but is having %s' % unknown_types)

    buffer_column_dtype = info_df[info_df.TYPE == required_buffer_type][
        _BUFFER_HEADER].dtype
    if not numpy.issubdtype(buffer_column_dtype, numpy.number):
        raise ValueError(
            'The %s attribute in Info CSV should be a number for stressors, '
            'and empty for habitats.' % _BUFFER_HEADER)

    # Convert all relative paths to absolute paths
    info_df['PATH'] = info_df.apply(
        lambda row: _to_abspath(
            row['PATH'], os.path.dirname(base_info_csv_path)), axis=1)
    # Check if the file on the path is a raster or vector
    info_df['IS_RASTER'] = info_df.apply(
        lambda row: _label_raster(row['PATH']), axis=1)
    # Get raster's linear unit, and raise an exception if projection is missing
    info_df['LINEAR_UNIT'] = info_df.apply(
        lambda row: _label_linear_unit(row), axis=1)
    # Generate simplified vector path if the file is a vector
    info_df['SIMPLE_VECTOR_PATH'] = info_df.apply(
        lambda row: _generate_vector_path(
            row, file_preprocessing_dir, 'simplified_', suffix_end), axis=1)
    # Generate raster paths which vectors will be rasterized onto
    info_df['BASE_RASTER_PATH'] = info_df.apply(
        lambda row: _generate_raster_path(
            row, file_preprocessing_dir, 'base_', suffix_end), axis=1)
    # Generate raster paths which will be aligned & resized from base rasters
    info_df['ALIGN_RASTER_PATH'] = info_df.apply(
        lambda row: _generate_raster_path(
            row, file_preprocessing_dir, 'aligned_', suffix_end), axis=1)
    # Generate distance raster paths which is transformed from aligned rasters
    info_df['DIST_RASTER_PATH'] = info_df.apply(
        lambda row: _generate_raster_path(
            row, file_preprocessing_dir, 'dist_', suffix_end), axis=1)

    # Generate raster paths with exposure and consequence suffixes.
    for column_name, suffix_front in {
            'FINAL_E_RASTER_PATH': 'TOT_E_',
            'FINAL_C_RASTER_PATH': 'TOT_C_'}.iteritems():
        info_df[column_name] = info_df.apply(
            lambda row: _generate_raster_path(
                row, intermediate_dir, suffix_front, suffix_end), axis=1)

    # Generate cumulative risk raster paths with risk suffix.
    info_df['TOTAL_RISK_RASTER_PATH'] = info_df.apply(
        lambda row: _generate_raster_path(
            row, intermediate_dir, 'TOT_R_', suffix_end), axis=1)

    # Generate reclassified risk raster paths with risk suffix.
    info_df['RECLASS_RISK_RASTER_PATH'] = info_df.apply(
        lambda row: _generate_raster_path(
            row, output_dir, 'risk_', suffix_end), axis=1)

    habitat_names = info_df[info_df.TYPE == 'habitat'].NAME.tolist()
    stressor_names = info_df[info_df.TYPE == 'stressor'].NAME.tolist()

    return info_df, habitat_names, stressor_names


def _get_criteria_dataframe(criteria_csv_path):
    """Get validated criteria dataframe from a given path to a CSV file.

    Parameters:
        criteria_csv_path (str): a path to the CSV file with habitat and
            stressor criteria ratings.

    Returns:
        criteria_df (dataframe): a dataframe converted from CSV with 'nan' for
            empty cells.

    Raises:
        ValueError when any required index or column header is missing from the
            CSV file.

    """

    # Read the CSV file into dataframe, with first column as index.
    # Column names are auto-generated ordinal values
    criteria_df = pandas.read_csv(criteria_csv_path, index_col=0, header=None)
    # Convert empty cells to None (i.e. None)
    criteria_df.index = [x if isinstance(x, str) else None
                         for x in criteria_df.index]

    # Verify the values in the index column, and append to error message if
    # there's any missing index
    required_indexes = [_HABITAT_NAME_HEADER, _HABITAT_RESILIENCE_HEADER,
                        _HABITAT_STRESSOR_OVERLAP_HEADER]

    missing_indexes = set(required_indexes) - set(criteria_df.index.values)

    if missing_indexes:
        raise ValueError('The Criteria CSV file is missing the following '
                         'value(s) in the first column: %s.\n' %
                         list(missing_indexes))

    # Validate the column header, which should have 'criteria type'
    criteria_df.columns = [
        x if isinstance(x, str) else None for x in
        criteria_df.loc[_HABITAT_NAME_HEADER].values]
    if _CRITERIA_TYPE_HEADER not in criteria_df.columns.values:
        raise ValueError('The Criteria CSV file is missing the column header'
                         ' "%s".' % _CRITERIA_TYPE_HEADER)

    return criteria_df


def _get_attributes_from_df(criteria_df, habitat_names, stressor_names):
    """Get habitat names, resilience attributes, stressor attributes info.

    Get the info from the criteria dataframe.

    Parameters:
        criteria_df (dataframe): a validated dataframe with required
            fields in it.

        habitat_names (list): a list of habitat names obtained from info CSV.

        stressor_names (list): a list of stressor names obtained from info CSV.

    Returns:
        resilience_attributes (list): a list of resilience attributes used for
            getting rating, dq, and weight for each attribute.

        stressor_attributes (dict): a dictionary with stressor names as keys,
            and a list of overlap properties (strings) as values.

    Raises:
        ValueError if criteria_df does not have names from habitat_names and
            stressor_names.

        ValueError if a stressor criteria shows up before any stressor.

    """
    # Get habitat names from the first row
    missing_habitat_names = list(
        set(habitat_names) - set(criteria_df.columns.values))

    missing_stressor_names = list(
        set(stressor_names) - set(criteria_df.index.values))

    missing_names_error_message = ''
    if missing_habitat_names:
        missing_names_error_message += (
            'The following Habitats in the info CSV file are missing from the '
            'criteria CSV file: %s. ' % missing_habitat_names)
    if missing_stressor_names:
        missing_names_error_message += (
            'The following Stresors in the info CSV file are missing from the '
            'criteria CSV file: %s' % missing_stressor_names)
    if missing_names_error_message:
        raise ValueError(missing_names_error_message)

    # Get habitat resilience attributes
    resilience_attributes = []
    found_resilience_header = False

    for idx, field in enumerate(criteria_df.index.values):
        # Make an empty list when the habitat resilience header shows up
        if field == _HABITAT_RESILIENCE_HEADER:
            found_resilience_header = True
            continue

        # Add the field to the list if it's after the resilience header and
        # before the overlap header
        if found_resilience_header:
            if field != _HABITAT_STRESSOR_OVERLAP_HEADER:
                # Append the field if the cell is not empty
                if field is not None:
                    resilience_attributes.append(field)
            else:
                # Get the index of overlap header when it's reached
                last_idx = idx
                break

    LOGGER.info('resilience_attributes: %s' % resilience_attributes)

    # Make a dictionary of stressor (key) with its attributes (value)
    stressor_attributes = {}
    # Enumerate from the overlap header
    stressor_overlap_indexes = criteria_df.index.values[(last_idx+1):]
    current_stressor = None
    for idx, field in enumerate(stressor_overlap_indexes):
        if field is not None:
            if field in stressor_names:
                # Set the current stressor to the encountered stressor and
                # add it to the attributes table
                current_stressor = field
                stressor_attributes[current_stressor] = []
                continue

            # Append the field as a stressor attribute if it's not a stressor
            elif current_stressor:
                stressor_attributes[current_stressor].append(field)

            # Raise an exception if a criteria shows up before a stressor
            else:
                raise ValueError('The "%s" criteria does not belong to any '
                                 'stressors. Please check your criteria CSV.'
                                 % field)

    LOGGER.info('stressor_attributes: %s' % stressor_attributes)

    return resilience_attributes, stressor_attributes


def _validate_rating(
        rating, max_rating, criteria_name, habitat, stressor=None):
    """Validate rating value, which should range between 1 to maximum rating.

    Parameters:
        rating (str): a string of either digit or file path. If it's a digit,
            it should range between 1 to maximum rating.

        max_rating (float): a number representing the highest value that
            is represented in criteria rating.

        criteria_name (str): the name of the criteria attribute where rating
            is from.

        habitat (str): the name of the habitat where rating is from.

        stressor (str): the name of the stressor where rating is from. Can be
            None when we're checking the habitat-only attributes. (optional)

    Returns:
        _rating_lg_one (bool): a value indicating if the rating is at least 1.

    Raises:
        A ValueError if the rating score is larger than the maximum rating that
            the user indicates.

    """
    _rating_lg_one = True

    if rating.isdigit():
        # If rating is less than 1, ignore this criteria attribute
        if float(rating) < 1:
            _rating_lg_one = False
            warning_message = '"%s" for habitat %s' % (criteria_name, habitat)

            if stressor:
                warning_message += (' and stressor %s' % stressor)

            warning_message += (
                ' has a rating %s less than 1, so this criteria attribute is '
                'ignored in calculation.' % rating)

            LOGGER.warning(warning_message)

        # Raise an exception if rating is larger than the maximum rating that
        # the user specified
        elif float(rating) > float(max_rating):
            error_message = '"%s" for habitat %s' % (criteria_name, habitat)

            if stressor:
                error_message += (' and stressor %s' % stressor)

            error_message += (
                ' has a rating %s larger than the maximum rating %s. '
                'Please check your criteria CSV file.' % (rating, max_rating))

            raise ValueError(error_message)

    return _rating_lg_one


def _validate_dq_weight(dq, weight, habitat, stressor=None):
    """Check if DQ and Weight column values are numbers.

    Parameters:
        dq (str): a string representing the value of data quality score.

        weight (str): a string representing the value of weight score.


        habitat (str): the name of the habitat where the score is from.

        stressor (str): the name of the stressor where the score is from. Can
            be None when we're checking the habitat-only attributes. (optional)

    Returns:
        None

    Raises:
        ValueError if the value of the DQ or weight is not a number.

    """
    for key, value in {
            'DQ': dq,
            'Weight': weight}.iteritems():

        # The value might be NaN or a string of digit, therefore check for
        # both cases
        if (isinstance(value, (float, int)) and numpy.isnan(value)) or \
                not value.isdigit():
            error_message = (
                'Values in the %s column for habitat "%s" ' % (key, habitat))

            # Format the error message based on whether stressor name is given
            if stressor:
                error_message += 'and stressor "%s"' % stressor

            error_message += ' should be a number, but is "%s".' % value

            raise ValueError(error_message)


def _get_overlap_dataframe(criteria_df, habitat_names, stressor_attributes,
                           max_rating, inter_dir, output_dir, suffix):
    """Return a dataframe based on habitat-stressor overlap properties.

    Calculation on exposure or consequence score will need or build information
    on numerator, denominator, spatially explicit criteria dict, final score
    raster path, numerator raster path, and mean score statistics dict. The
    spatially explicit criteria scores will be added to the score calculation
    later on.

    Parameters:
        criteria_df (dataframe): a validated dataframe with required
            fields.

        habitat_names (list): a list of habitat names used as dataframe index.

        stressor_attributes (dict): a dictionary with stressor names as keys,
            and a list of overlap criteria (strings) as values.

        max_rating (float): a number representing the highest value that
            is represented in criteria rating.

        inter_dir (str): a path to the folder where numerator/denominator E/C
            paths will be created in.

        output_dir (str): a path to the folder where E/C raster paths will be
            created in.

        suffix (str): a file suffix to append to the end of filenames.

    Returns:
        overlap_df (dataframe): a multi-index dataframe with E/C scores for
            each habitat and stressor pair.

    Raises:
        ValueError if the value of the criteria type column from criteria_df
            is not either E or C.

        ValueError if the value of the rating column from criteria_df is less
            than 1 or larger than the maximum rating.

        ValueError if the value of the DQ or weight column from criteria_df is
            not a number or is a number less than 1.

        ValueError if any stressor-habitat does not have at least one E and C
            criteria rating.

    """
    # Create column headers to keep track of data needed to calculate
    # exposure and consequence scores
    column_headers = ['_NUM', '_DENOM', '_SPATIAL', '_RASTER_PATH',
                      '_NUM_RASTER_PATH', '_PICKLE_STATS_PATH', '_MEAN']
    overlap_column_headers = map(
        str.__add__,
        ['E']*7 + ['C']*7, column_headers*2)

    # Create an empty dataframe, indexed by habitat-stressor pairs.
    stressor_names = stressor_attributes.keys()
    multi_index = pandas.MultiIndex.from_product(
        [habitat_names, stressor_names], names=['habitat', 'stressor'])
    LOGGER.info('multi_index: %s' % multi_index)

    # Create a multi-index dataframe and fill in default cell values
    overlap_df = pandas.DataFrame(
        # Data values on each row corresponds to each column header
        data=[[0, 0, {}, None, None, None, {},
               0, 0, {}, None, None, None, {}]
              for i in xrange(len(habitat_names)*len(stressor_names))],
        columns=overlap_column_headers, index=multi_index)

    # Start iterating from row indicating the beginning of habitat and stressor
    # overlap criteria
    stressor = None
    for row_idx, row_data in criteria_df.loc[
            _HABITAT_STRESSOR_OVERLAP_HEADER:].iterrows():
        if row_idx in stressor_attributes:
            # Start keeping track of the number of overlap criteria used
            # for a stressor found from the row
            stressor = row_idx

        # If stressor exists and the row index is not None
        elif stressor and row_idx:
            criteria_name = row_idx
            criteria_type = row_data[_CRITERIA_TYPE_HEADER].upper()
            if criteria_type not in ['E', 'C']:
                raise ValueError('Criteria Type in the criteria scores CSV '
                                 'table should be either E or C.')

            for idx, (row_key, row_value) in enumerate(row_data.iteritems()):
                # The first value in the criteria row should be a rating value
                # with habitat name as key, after a stressor was found
                if idx % 3 == 0:
                    if row_key in habitat_names:
                        habitat = row_key

                        # Create E or C raster paths on habitat-stressor pair
                        overlap_df.loc[
                            (habitat, stressor),
                            criteria_type + '_RASTER_PATH'] = os.path.join(
                                output_dir, criteria_type + '_' + habitat
                                + '_' + stressor + suffix + '.tif')
                        overlap_df.loc[
                            (habitat, stressor),
                            criteria_type + '_NUM_RASTER_PATH'] = os.path.join(
                                inter_dir, criteria_type + '_num_' +
                                habitat + '_' + stressor + suffix + '.tif')

                        # Create individual habitat-stressor risk raster path
                        overlap_df.loc[
                            (habitat, stressor),
                            'PAIR_RISK_RASTER_PATH'] = os.path.join(
                                output_dir, 'R_' +
                                habitat + '_' + stressor + suffix + '.tif')

                        # Create pickle file path that stores zonal stats dict
                        overlap_df.loc[
                            (habitat, stressor), criteria_type +
                            '_PICKLE_STATS_PATH'] = os.path.join(
                                inter_dir, criteria_type + '_' +
                                habitat + '_' + stressor + suffix + '.pickle')

                        # If rating is less than 1, skip this criteria row
                        rating = row_value
                        if not _validate_rating(
                            rating, max_rating, criteria_name, habitat,
                                stressor):
                            continue

                    # If the first value is not a rating value, break the loop
                    # since it has reaches the end of the criteria row
                    else:
                        break

                # The second value in the row should be data quality (dq) for
                # that habitat-stressor criteria
                elif idx % 3 == 1:
                    dq = row_value

                # The third value in the row should be weight for the habitat-
                # stressor criteria
                else:
                    weight = row_value

                    # Check the DQ and weight values when we have collected
                    # both of them
                    _validate_dq_weight(dq, weight, habitat, stressor)

                    # Calculate the cumulative numerator score
                    if rating.isdigit():
                        overlap_df.loc[(habitat, stressor),
                                       criteria_type + '_NUM'] += \
                            float(rating)/float(dq)/float(weight)

                    # Save the rating, dq, and weight to the spatial criteria
                    # dictionary in the dataframe if rating is not a number
                    else:
                        overlap_df.loc[
                            (habitat, stressor),
                            criteria_type + '_SPATIAL']['_'.join(
                                [habitat, stressor,
                                 criteria_name])] = [
                                rating, dq, weight]

                    # Calculate the cumulative denominator score
                    overlap_df.loc[
                        (habitat, stressor), criteria_type + '_DENOM'] += \
                        1/float(dq)/float(weight)

    # If any stressor-habitat doesn't have at least one E or C criteria rating,
    # raise an exception
    for criteria_type, criteria_type_long in {
            'E': 'exposure', 'C': 'consequence'}.iteritems():
        if (overlap_df[criteria_type + '_DENOM'] == 0).any():
            raise ValueError(
                'The following stressor-habitat pair(s) do not have at least '
                'one %s rating: %s' % (criteria_type_long, overlap_df[
                    overlap_df[criteria_type + '_DENOM'] == 0].index.tolist()))

    LOGGER.info('overlap_df: %s' % overlap_df)
    return overlap_df


def _get_recovery_dataframe(criteria_df, habitat_names, resilience_attributes,
                            max_rating, inter_dir, output_dir, suffix):
    """Return a dataframe with calculated habitat resilience scores.

    The calculation of recovery score will need or build information on
    numerator, denominator, spatially explicit criteria dict, score raster
    path, and numerator raster path.

    Parameters:
        criteria_df (dataframe): a validated dataframe with required
            fields.

        habitat_names (list): a list of habitat names used as dataframe index.

        resilience_attributes (list): a list of resilience attributes used for
            getting rating, dq, and weight for each attribute.

        max_rating (float): a number representing the highest value that
            is represented in criteria rating.

        inter_dir (str): a path to the folder where numerator/denominator
            scores for recovery potential paths will be created in.

        output_dir (str): a path to the folder where recovery raster paths will
            be created in.

        suffix (str): a file suffix to append to the end of filenames.

    Returns:
        recovery_df (dataframe): the dataframe with recovery information for
            each habitat.

    Raises:
        ValueError if the value of the rating column from criteria_df is less
            than 1 or larger than the maximum rating.

        ValueError if the value of the DQ or weight column from criteria_df is
            not a number or is a number less than 1.

    """
    # Create column headers to keep track of data needed to calculate recovery
    # scores for each habitat
    recovery_column_headers = [
        'R_NUM', 'R_DENOM', 'R_SPATIAL', 'R_RASTER_PATH', 'R_NUM_RASTER_PATH']

    # Create the dataframe whose data is 0 for numerators and denominators,
    # None for raster paths, and an empty dict for spatially explicit criteria.
    recovery_df = pandas.DataFrame(
        data=[[0, 0, {}, None, None] for i in xrange(len(habitat_names))],
        index=habitat_names, columns=recovery_column_headers)

    i = 0
    # The loop through the column headers that has habitat names in itself
    while i < len(criteria_df.columns.values):
        # If the column header is in the habitat list, get the habitat name,
        # which will be used as index in recovery_df
        if criteria_df.columns.values[i] in habitat_names:
            habitat = criteria_df.columns.values[i]
            # Create recovery raster paths for later calculation
            recovery_df.loc[habitat, 'R_NUM_RASTER_PATH'] = os.path.join(
                inter_dir, 'RECOV_num_' + habitat + suffix + '.tif')

            recovery_df.loc[habitat, 'R_RASTER_PATH'] = os.path.join(
                output_dir, 'recovery_' + habitat + suffix + '.tif')

            # Calculate cumulative numerator and denominator scores based on
            # each habitat's resilience rating, dq, and weight
            for resilience_attr in resilience_attributes:
                rating = criteria_df.loc[resilience_attr, habitat]
                dq = criteria_df.loc[resilience_attr][i+1]
                weight = criteria_df.loc[resilience_attr][i+2]

                # Check the DQ and weight values
                _validate_dq_weight(dq, weight, habitat)

                # If rating is less than 1, skip this criteria row
                if not _validate_rating(
                        rating, max_rating, resilience_attr, habitat):
                    continue

                # If rating is a number, calculate the numerator score
                if rating.isdigit():
                    recovery_df.loc[habitat, 'R_NUM'] += \
                        float(rating)/float(dq)/float(weight)
                else:
                    # If rating is based a spatial file, store the file path,
                    # dq, and weight in the dictionary
                    recovery_df.loc[habitat, 'R_SPATIAL'][
                        habitat + '_' + resilience_attr] = [rating, dq, weight]

                # Add 1/(dq*w) to the denominator
                recovery_df.loc[habitat, 'R_DENOM'] += 1/float(dq)/float(weight)

            i += 3  # Jump to next habitat
        else:
            i += 1  # Keep finding the next habitat from the habitat list

    LOGGER.info('recovery_df: %s' % recovery_df)
    return recovery_df


def _simplify_geometry(
        base_vector_path, tolerance, target_simplified_vector_path,
        preserved_field=None):
    """Simplify all the geometry in the vector.

    See https://en.wikipedia.org/wiki/Nyquist%E2%80%93Shannon_sampling_theorem
    for the math prove.

    Parameters:
        base_vector_path (string): path to base vector.

        tolerance (float): all new vertices in the geometry will be within
            this distance (in units of the vector's projection).

        target_simplified_vector_path (string): path to desired simplified
            vector.

        preserved_field (tuple): a tuple of field name (string) and field type
            (OGRFieldType) that will remain in the target simplified vector.

    Returns:
        None

    """
    base_vector = ogr.Open(base_vector_path)
    base_layer = base_vector.GetLayer()
    base_layer_defn = base_layer.GetLayerDefn()
    target_field_name = None
    if preserved_field:
        for i in range(base_layer_defn.GetFieldCount()):
            base_field_name = base_layer_defn.GetFieldDefn(i).GetName()
            if base_field_name == preserved_field[0]:
                # Create a target field definition
                target_field_name = preserved_field[0]
                target_field = ogr.FieldDefn(
                    base_field_name, preserved_field[1])
                break

    # Convert a unicode string into UTF-8 standard to avoid TypeError when
    # creating layer with the basename
    target_layer_name = os.path.splitext(
        os.path.basename(target_simplified_vector_path))[0]
    target_layer_name = target_layer_name.encode('utf-8')

    if os.path.exists(target_simplified_vector_path):
        os.remove(target_simplified_vector_path)

    gpkg_driver = ogr.GetDriverByName('GPKG')

    target_simplified_vector = gpkg_driver.CreateDataSource(
        target_simplified_vector_path)
    target_simplified_layer = target_simplified_vector.CreateLayer(
        target_layer_name,
        base_layer.GetSpatialRef(), ogr.wkbPolygon)

    target_simplified_layer.StartTransaction()

    if target_field_name:
        target_simplified_layer.CreateField(target_field)

    for feature in base_layer:
        target_feature = ogr.Feature(target_simplified_layer.GetLayerDefn())
        feature_geometry = feature.GetGeometryRef()

        # Use SimplifyPreserveTopology to prevent features from missing
        simplified_geometry = feature_geometry.SimplifyPreserveTopology(
            tolerance)
        feature_geometry = None
        if (simplified_geometry is not None and
                simplified_geometry.GetArea() > 0):
            target_feature.SetGeometry(simplified_geometry)
            # Set field value to the field name that needs to be preserved
            if target_field_name:
                field_value = feature.GetField(target_field_name)
                target_feature.SetField(target_field_name, field_value)
            target_simplified_layer.CreateFeature(target_feature)
    target_simplified_layer.CommitTransaction()
    target_simplified_layer.SyncToDisk()
    target_simplified_layer = None
    target_simplified_vector.SyncToDisk()
    target_simplified_vector = None


@validation.invest_validator
def validate(args, limit_to=None):
    """Validate args to ensure they conform to `execute`'s contract.

    Parameters:
        args (dict): dictionary of key(str)/value pairs where keys and
            values are specified in `execute` docstring.
        limit_to (str): (optional) if not None indicates that validation
            should only occur on the args[limit_to] value. The intent that
            individual key validation could be significantly less expensive
            than validating the entire `args` dictionary.

    Returns:
        list of ([invalid key_a, invalid_keyb, ...], 'warning/error message')
            tuples. Where an entry indicates that the invalid keys caused
            the error message in the second part of the tuple. This should
            be an empty list if validation succeeds.

    """
    missing_key_list = []
    no_value_list = []
    validation_error_list = []
    max_rating_key = 'max_rating'
    aoi_vector_key = 'aoi_vector_path'

    for key in [
            'workspace_dir',
            'info_csv_path',
            'criteria_csv_path',
            'resolution',
            'max_rating',
            'risk_eq',
            'decay_eq',
            'aoi_vector_path']:
        if limit_to is None or limit_to == key:
            if key not in args:
                missing_key_list.append(key)
            elif args[key] in ['', None]:
                no_value_list.append(key)

    if missing_key_list:
        # if there are missing keys, we have raise KeyError to stop hard
        raise KeyError(
            "The following keys were expected in `args` but were missing: " +
            ', '.join(missing_key_list))

    if no_value_list:
        validation_error_list.append(
            (no_value_list, 'parameter has no value'))

    for key in [
            'criteria_csv_path', 'info_csv_path']:
        if (limit_to is None or limit_to == key) and (
                not os.path.exists(args[key])):
            validation_error_list.append(
                ([key], 'not found on disk'))

    for key, key_values in {
            'risk_eq': ['Euclidean', 'Multiplicative'],
            'decay_eq': ['Linear', 'Exponential', 'None']}.iteritems():
        if limit_to is None or limit_to == key:
            if args[key] not in key_values:
                validation_error_list.append(
                    ([key], 'should be one of the following: %s, but is "%s" '
                     'instead' % (key_values, args[key])))

    if limit_to is None or limit_to == max_rating_key:
        # If the argument isn't a number, check if it can be converted to a
        # number
        if not isinstance(args[max_rating_key], (int, float)):
            if args[max_rating_key].lstrip("-").isdigit():
                max_rating_value = float(args[max_rating_key])
            else:
                validation_error_list.append(
                    ([max_rating_key], 'should be a number'))
        else:
            max_rating_value = args[max_rating_key]

        # If the argument is a number, check if it's larger than 1
        if 'max_rating_value' in locals() and max_rating_value <= 1:
            validation_error_list.append(
                ([max_rating_key], 'should be larger than 1'))

    # check that existing/optional files are the correct types
    with utils.capture_gdal_logging():
        if ((limit_to is None or limit_to == aoi_vector_key) and
                aoi_vector_key in args):
            if not os.path.exists(args[aoi_vector_key]):
                validation_error_list.append(
                    ([aoi_vector_key], 'not found on disk'))

            vector = gdal.OpenEx(args[aoi_vector_key])
            if vector is None:
                validation_error_list.append(
                    ([aoi_vector_key], 'not a vector'))
            else:
                vector = None

    return validation_error_list
