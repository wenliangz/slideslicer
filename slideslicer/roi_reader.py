import re
import json
import numpy as np
import pandas as pd
import openslide
import shapely
from shapely import affinity
from shapely.geometry import Polygon, MultiPolygon, MultiLineString
from descartes import PolygonPatch
import matplotlib.pyplot as plt
from matplotlib import colors
from itertools import cycle
from warnings import warn
from .slideutils import (get_vertices, get_roi_dict, get_median_color,
                        get_threshold_tissue_mask, convert_mask2contour,
                        get_thumbnail_magnification, plot_contour)

from .parse_leica_xml import parse_xml2annotations
from .geom_tools import resolve_selfintersection
from .slideutils import sample_points, CentredRectangle


def _get_patch_(slide, xc, yc,
              patch_size = [1024, 1024],
              magn_base = 4,
              scale = 2,
             ):
    """retrieve a patch from openslide with given center point, size, and subsampling rate
    currently tested only on Leica SVS slides"""
    if scale>0:
        target_subsample = max(scale, 1/scale)
    else:
        target_subsample = - scale
    exp_raw = np.log2(target_subsample)/np.log2(magn_base)
    magn_exp = int(np.floor(exp_raw))
    subsample = magn_base**-(exp_raw-magn_exp)

    size_ = [ps//(magn_base**magn_exp) for ps in patch_size]
    region_ = slide.read_region((int(xc-patch_size[0]//2), int(yc-patch_size[1]//2)), magn_exp, size_)
    if subsample!= 1.0:
        region_ = region_.resize([int(subsample * s) for s in region_.size], 
                                 openslide.Image.ANTIALIAS)
        #region_ = np.asarray(region_)[...,:3]
        #region_ = cv2.resize(region_, (0,0), fx=subsample, fy=subsample,
        #                        interpolation = cv2.INTER_AREA)
    return region_


def find_chunk_content(roilist):
    """finds features (gloms, infl, etc) contained within tissue chunks.
    Returns a dictionary:
    {tissue_chunk_1_id: [feature_1_id, ..., feature_n_id],
     tissue_chunk_1_id: [...]
    }
    Requires `shapely` package
    """
    pgs_tissue = {}
    pgs_feature = {}
    for roi in roilist:
        if roi["name"]=="tissue":
            pgs_tissue[roi['id']] = Polygon(roi["vertices"])
        else:
            pgs_feature[roi['id']] = Polygon(roi["vertices"])

    tissue_contains = dict(zip(pgs_tissue.keys(), [[] for _ in range(len(pgs_tissue))]))
    remove_items = []
    for idt, pt in pgs_tissue.items():
        for idf in remove_items:
            pgs_feature.pop(idf)
        remove_items = []
        for idf, pf in pgs_feature.items():
            if pt.intersects(pf):
                remove_items.append(idf)
                tissue_contains[idt].append(idf)
    return tissue_contains


def remove_empty_tissue_chunks(roilist):
    """removes tissue chunks that contain no annotation contours within"""
    chunk_content = find_chunk_content(roilist)
    empty_chunks = set([kk for kk,vv in chunk_content.items() if len(vv)==0])
    return [roi for roi in roilist if roi['id'] not in empty_chunks]


class RoiReader():
    """a generic class for matched annotations-slide reading and processing 
    """
    def __init__(self, inputfile, threshold_tissue=True, remove_empty=True,
                  save=True, outdir=None, minlen=50,
                  annotation_format = 'leica',
                  slide_format = 'leica',
                  verbose=True):
        """
        extract and save rois

        Inputs:
        inputfile     -- xml or svs file path
        remove_empty  -- remove empty chunks of tissue
        outdir        -- (optional); save into an alternative directory
        minlen        -- minimal length of tissue chunk contour in thumbnail image
        keeplevels    -- number of file path elements to keep 
                         when saving to provided `outdir`
                         (1 -- filename only; 2 -- incl 1 directory)
        """
        self.filenamebase = re.sub('.svs$','', re.sub(".xml$", "", inputfile))
        self.verbose = verbose
        ############################
        # parsing annotations
        ############################
        self.slide_format = slide_format
        if annotation_format == 'leica':
            fnxml = self.filenamebase + '.xml'
            try:
                self.rois = parse_xml2annotations(fnxml)
                for roi in self.rois:
                    roi["name"] = roi.pop("text").lower().rstrip('.')
            except:
                warn('ROI file not found; (supposedly "{}")'.format(fnxml))
        else:
            NotImplementedError('format "%s" is not supported yet' % annotation_format)
            
        # for an ellipse, 
        #    area = $\pi \times r \times R$

        if threshold_tissue:
            self.add_tissue(remove_empty=remove_empty,
                   color=False, filtersize=7, minlen=minlen)

        if save:
            self.save()

    @property 
    def thumbnail(self):
        return self.load_thumbnail()

    def load_thumbnail(self):
        slide = self.slide
        self.img = np.asarray(slide.associated_images["thumbnail"])
        self.width, self.height = slide.dimensions
        self.median_color = get_median_color(slide)
        self._thumbnail_ratio = get_thumbnail_magnification(slide)
        return self.img

    @property
    def slide(self):
        fnsvs = self.filenamebase + ".svs"
        slide_ = openslide.OpenSlide(fnsvs)
        return slide_


    def extract_tissue(self, color=False, filtersize=7, minlen=50):
        ## Extract tissue chunk ROIs
        self.load_thumbnail()

        ## Extract mask and contours
        mask = get_threshold_tissue_mask(self.img, color=color, filtersize=filtersize)
        contours = convert_mask2contour(mask, minlen=minlen)


        sq_micron_per_pixel = np.median([roi["areamicrons"] / roi["area"] 
                                        for roi in self.rois])

        self.tissue_rois = [get_roi_dict(cc*self._thumbnail_ratio,
                                        name='tissue', id=1+nn+len(self.rois),
                                        sq_micron_per_pixel=sq_micron_per_pixel) 
                            for nn,cc in enumerate(contours)]
        return self.tissue_rois 


    def add_tissue(self, remove_empty=True,
                   color=False, filtersize=7, minlen=50):
                   
        if not hasattr(self, 'tissue_rois'):
            self.extract_tissue(color=color, filtersize=filtersize, minlen=minlen) 

        self.rois = self.rois + self.tissue_rois

        if self.verbose:
            print('-'*15)
            print("counts of ROIs")
            print('-'*15)
            roi_name_counts = (pd.Series([rr["name"] for rr in self.rois])
                              .value_counts() )
            print(roi_name_counts)
        
        if remove_empty:
            self.rois = remove_empty_tissue_chunks(self.rois)

            if self.verbose:
                print('-'*45)
                print("counts of ROIs after removing empty chunks")
                print('-'*45)
                roi_name_counts = pd.Series([rr["name"] for rr in self.rois]).value_counts()
                print(roi_name_counts)

    @property
    def df(self):
        if not hasattr(self, '_df'):
            self._df = pd.DataFrame(self.rois)
            self._df['polygon'] = self._df['vertices'].map(Polygon)
            self._df['polygon'] = self._df['polygon'].map(resolve_selfintersection)
        return self._df

    @property
    def df_tissue(self):
        return self.df[self._df.name=='tissue']

    @classmethod
    def resolve_multipolygons(cls, df):
        mask_multipolygon = df.polygon.map(lambda mpg: isinstance(mpg, MultiPolygon))
        if mask_multipolygon.sum()>0:
            df_mpg = pd.merge(
                         df[mask_multipolygon].drop('polygon', axis=1),
                         (df[mask_multipolygon][['polygon']]
                              .apply(lambda x: x.apply(pd.Series).stack())
                              .reset_index(level=1, drop=True)),
                         right_index=True, left_index=True)
            df = pd.concat([df[~mask_multipolygon], df_mpg])
        mask_multistr = df.polygon.map(lambda x: isinstance(x.boundary, MultiLineString))
        if mask_multistr.sum()>0:
            df.loc[mask_multistr,'polygon'] = df.loc[mask_multistr,'polygon'].map(
                    lambda pg: Polygon(pg.boundary[np.argmax(map(len, pg.boundary))]))
        return df

    def __getitem__(self, key):
        return self.df.iloc[key]

    def get_patch_rois(self, xc, yc, patch_size, scale=1,
                       translate=True, **kwargs):
        if 'target_subsample' in kwargs:
            scale = kwargs.pop('target_subsample')
            warn('deprication warning', DeprecationWarning)
        if isinstance(patch_size, int):
            patch_size = [patch_size]*2
        patch_size = [x for x in patch_size]
        patch = CentredRectangle(xc, yc, *patch_size)
        mask = self.df['polygon'].map(lambda x: patch.intersects(x))
        df = self.df[mask].copy()
        df.loc[:,'polygon'] = df['polygon'].map(lambda x: patch & x)
        if translate:
            def shift(x):
                return affinity.translate(x, -patch.bounds[0], -patch.bounds[1])
            df.loc[:,'polygon'] = df['polygon'].map(shift)
        if scale != 1:
            def scale_(x, translate=translate):
                origin = (0,0) if translate else (xc, yc)
                return affinity.scale(x, 1/scale, 1/scale, origin=origin)
            df.loc[:,'polygon'] = df['polygon'].map(scale_)
        df = RoiReader.resolve_multipolygons(df)
        df.loc[:,'vertices'] = df['polygon'].map(lambda p: np.asarray(p.boundary.coords.xy).T.tolist())
        df.loc[:,'area'] = df['polygon'].map(lambda p: p.area)
        return df


    def get_patch(self, xc, yc, patch_size, scale=1,
                  magn_base = 4, **kwargs):
        if 'target_subsample' in kwargs:
            scale = kwargs.pop('target_subsample')
            warn('deprication warning', DeprecationWarning)
        if isinstance(patch_size, int):
            patch_size = [patch_size]*2

        patch = _get_patch_(self.slide, xc, yc,
                            patch_size = patch_size,
                            magn_base = magn_base,
                            scale=scale)
        return patch    


    def plot_patch(self, xc, yc, patch_size, scale=1,
                   magn_base = 4, translate=True,
                   colordict = {}, figsize=None,
                   vis_scale=True,
                   fig=None, ax=None, alpha=0.1, **kwargs):

        if 'target_subsample' in kwargs:
            scale = kwargs.pop('target_subsample')
            warn('deprication warning', DeprecationWarning)
        if isinstance(patch_size, int) or isinstance(patch_size, float):
            patch_size = [int(patch_size)]*2

        patch = self.get_patch(xc, yc, patch_size, scale=scale, 
                               magn_base = magn_base,)

        prois = self.get_patch_rois(xc, yc, patch_size,
                                    scale=scale,
                                    translate=translate)
        if fig is None:
            if ax is not None:
                fig = ax.get_figure()
            else:
                if len(kwargs)>0 or figsize is not None:
                    fig, ax = plt.subplots(1, figsize=figsize, **kwargs)
                else:
                    fig = plt.gcf()
                    ax  = fig.gca()
        elif ax is None:
            ax = fig.gca()

        ccycle = plt.rcParams['axes.prop_cycle'].by_key()['color']
        ccycle = cycle(ccycle)

        out_patch_size = patch_size
        if scale != 1:
            if vis_scale:
                out_patch_size = [int(np.round(ps/scale)) for ps in patch_size]
                scale_ = lambda x: x
            else:
                def scale_(x):
                    origin = (0,0) if translate else (xc, yc)
                    return affinity.scale(x, scale, scale, origin=origin)

        print('out_patch_size', out_patch_size)

        if translate:
            extent = (0, out_patch_size[0], out_patch_size[1], 0)
        else:
            extent = (xc - out_patch_size[0]//2,
                      xc + out_patch_size[0]//2,
                      yc + out_patch_size[1]//2,
                      yc - out_patch_size[1]//2,
                      )
        print('extent', extent)
        ax.imshow(patch,
                  extent=extent)

        for name_, gg in prois.groupby('name'):
            flag = True
            if name_ in colordict:
                c = colordict[name_]
            else:
                c = next(ccycle)
            for _, pp_ in gg.iterrows():
                #print(pp_['name'])
                pp = pp_.polygon
                if scale !=1:
                    pp = scale_(pp)
                fc = list(colors.to_rgba(c))
                ec = list(colors.to_rgba(c))
                fc[-1] = alpha
                ax.add_patch(PolygonPatch(pp, fc=fc, ec=ec, lw=2, label=name_ if flag else None))
                flag = False
        ax.relim()
        ax.autoscale_view()
        return fig, ax, patch, prois


    def plot(self, fig=None, ax=None, labels=True, **kwargs):
        if not hasattr(self, 'image'):
            self.load_thumbnail()
        left = 0
        top = 0
        right, bottom = self.width, self.height
        if fig is None:
            if ax is not None:
                fig = ax.get_figure()
            else:
                fig, ax = plt.subplots(1, **kwargs)
        elif ax is None:
            ax = fig.gca()

        ax.imshow(self.img, extent=(left, right, bottom, top))

        ccycle = plt.rcParams['axes.prop_cycle'].by_key()['color']
        last_color = ccycle[-1]
        ccycle = cycle(ccycle[:-1])
        for kk,vv in self.df.groupby('name'):
            if kk == 'tissue':
                cc = [0.25]*3
                start = True
                for kr, roi in vv.iterrows():
                    label = '{} #{}'.format(kk, roi['id'])
                    vert = roi['vertices']
                    centroid = (sum((x[0] for x in vert)) / len(vert), sum((x[1] for x in vert)) / len(vert))
                    plot_contour(vert, label=kk if start else None, c=cc, ax=ax)
                    if labels:
                        ax.text(*centroid, label, color=last_color)
                    start = False 
            else:
                cc = next(ccycle)
                start = True
                for vert in vv['vertices']:
                    plot_contour(vert, label=kk if start else None, c=cc, ax=ax)
                    start = False 
                
        return fig, ax

    '''
    def _repr_png_(self):
        """ iPython display hook support
        :returns: png version of the image as bytes
        """
        from io import BytesIO
        #from PIL import Image
        b = BytesIO()
        #Image.fromarray(self.img).save(b, 'png')
        fig, _ = self.plot()
        fig.savefig(b, format='png')
        return b.getvalue()
    '''

    def save(self, outdir=None, keeplevels=1):
        fnjson = self.filenamebase + ".json"
        self.json_filename = fnjson

        if outdir is not None and os.path.isdir(outdir):
            fnjson = fnjson.split('/')[-keeplevels]
            fnjson = os.path.join(outdir, fnjson)
            os.makedirs(os.path.dirname(fnjson), exist_ok = True)

        ## Save both contour lists together
        with open(fnjson, 'w+') as fh:
            json.dump(self.rois, fh)
        return fnjson

    def __repr__(self):
        res = """{} ROIs\n\tfrom{};
        """.format(len(self), self.filenamebase + '.svs')
        return res

    def _repr_html_(self):
        roi_name_counts = pd.Series([rr["name"] for rr in self.rois]).value_counts()
        roi_name_counts.name = 'counts'
        roi_name_counts = roi_name_counts.to_frame()

        prefix = '<h2>{} ROIs\n</h2><p>\tfrom <pre>{}</pre>\n</p>'.format(len(self), self.filenamebase + '.svs')
        return prefix + roi_name_counts._repr_html_()


    def __len__(self):
        return len(self.rois)


class PatchIterator():
    def __init__(self, roireader, vertices, side=128, 
                 subsample=8, batch_size=4, preprocess=lambda x:x,
                 points=None,
                 oversample=1, mode='grid'):
        
        self.roireader = roireader
        self.side_magn = side*subsample
        if points is None:
            self.spacing = self.side_magn/oversample
            self.points = sample_points(vertices, spacing=self.spacing, mode=mode)
        else:
            self.points = points

        self.batch_size = batch_size
        self.subsample = subsample
        self.index = -1
        self.indices = np.arange(len(self.points))
        self.preprocess = preprocess

    def __len__(self):
        return int(np.ceil(len(self.points)/self.batch_size))
        
    def __getitem__(self, key):
        start = key*self.batch_size
        end = min(len(self.indices), (1+key)*self.batch_size)
        assert end>start
        batch_x = []
        coords = []
        for ind in range(start, end):
            pp = self.points[self.indices[ind]]
#             print(pp)
            patch = self.roireader.get_patch(*pp, [self.side_magn]*2, 
                                             target_subsample=self.subsample )
            patch = np.asarray(patch)[...,:3]
            patch = self.preprocess(patch)
            batch_x.append(patch)
            coords.append(pp)
        return np.stack(batch_x), np.stack(coords)
        
    def __iter__(self):
        return self
    
    def __next__(self):
        self.index += 1

        if self.index >= len(self):
            raise StopIteration

        return self[self.index]
