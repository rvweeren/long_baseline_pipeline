#!/usr/bin/env python

import numpy as np
import os
import sys
import glob
import argparse
import casacore.tables as casatb
import astropy.io.fits as pyfits
from astropy.wcs import WCS
import time
import scipy
from scipy import stats
import pickle
import losoto.h5parm as h5parm
import matplotlib
matplotlib.use('Agg')
import astropy
from matplotlib import pyplot as plt
import h5py
import aplpy
import bdsf
from astropy.io import ascii


CCRIT=1.6 
TSAMP=4.0    # time per sample. All times are in seconds. TSAMP is passed to NDPPP for writing
             # the parset, since solints in NDPPP parsets are in samples.

def loop3log (vis, pstr, cret = True):
    # write a log entry
    if '_A' in vis:
        vis = vis.replace('_A','')
    fo = open(vis+'_proc.log','a')
    fo.write('%s'%pstr)
    fo.write('\n' if cret else '')
    fo.close()
    sys.stdout.write(pstr)
    sys.stdout.write('\n' if cret else '')

def h5read (htab, solset, soltab):
    # h5 routine to read h5 files. This is done in a separate python call
    # otherwise the hparm does not close properly. No info found on any other
    # way to do this.
    fo = open('tmp_read.py','w')
    fo.write ('import losoto.h5parm as h5parm,os,pickle\n')
    fo.write ('tab = h5parm.openSoltab(\'%s\',solsetName=\'%s\',soltabName=\'%s\')\n' % \
                          (htab,solset,soltab)   )
    fo.write ('v, vm = tab.getValues()[0], tab.getValues()[1]\n')
    fo.write ('pickle.dump(v,open(\'v.pkl\',\'wb\'))\n')
    fo.write ('pickle.dump(vm,open(\'vm.pkl\',\'wb\'))\n')
    fo.close()
    os.system ('python tmp_read.py')
    v = pickle.load (open('v.pkl','rb'))
    vm = pickle.load (open('vm.pkl','rb'))
    os.system('rm tmp_read.py;rm v.pkl;rm vm.pkl')
    return v, vm

def zerosol (vis,H1,ant):
    # Return a solution to zero (and ones if an amplitude solution exists).
    # Used after detection of an incoherent solution on an antenna.
    h1 = h5py.File(H1,'r+')
    n1 = h1.get('sol000/phase000')
    v1 = np.array(n1['val'])
    z = np.zeros_like(v1[:,:,0,:])
    ant1 = np.array(h1.get('sol000/phase000/ant'))
    loop3log (vis, 'Solutions present and zeroed: ',cret=False)
    for i in range(len(ant1)):
        if ant1[i] in ant:
            try:
                h1['sol000/amplitude000/val'][:,:,i,:] = z+1.0
            except:
                pass
            h1['sol000/phase000/val'][:,:,i,:] = z
            loop3log (vis,'%d '%i,cret=False)
    loop3log (vis,' ')
    h1.close()

def clcal (vis,H1,H2,ant_interp=None):
    # Given two calibration structures H1 and H2, and antennas to interpolate, 
    # replace the phase calibration of H1 for each antenna with an interpolated 
    # version of H2. (Has been tested for phase, needs testing for amplitude)
    isamp = True
    h1,h2 = h5py.File(H1,'r+'),h5py.File(H2)
    n1,n2 = h1.get('sol000/phase000'),h2.get('sol000/phase000')
    t1,t2 = np.array(n1['time']),np.array(n2['time'])
    v1,v2 = np.array(n1['val']),np.array(n2['val'])
    for i in ant_interp:
        loop3log (vis,i)
    loop3log (vis,'Interpolating %s:'%H2)
    for i in v2[0,0,:,0]:
        loop3log (vis,('%6.2f'%i) if ~np.isnan(i) else 'nan', cret=False)
    loop3log (vis,'\nInto %s:'%H1)
    for i in v1[0,0,:,0]:
        loop3log (vis,('%6.2f'%i) if ~np.isnan(i) else 'nan', cret=False)
    a1 = np.array(h1.get('sol000/phase000/ant'))
    try:
        na1,na2 = h1.get('sol000/amplitude000'),h2.get('sol000/amplitude000')
        va1,va2 = np.array(na1['val']),np.array(na2['val'])
    except:
        isamp = False
    ant_interp = a1 if ant_interp is None else ant_interp
    for i in range(len(a1)):
        # SM: Crashed here for 3C 273 (but not for 3C 280). Traceback:
        # File "/data020/scratch/sean/fafa/lofar-lb/loop3_serviceB.py", line 72, in clcal
        # for i in range(len(a1)):
        # ValueError: The truth value of an array with more than one element is ambiguous. Use a.any() or a.all()
        if a1[i] not in ant_interp:
            continue
        for iz in range(v1.shape[1]):
            for ipol in range(v1.shape[3]):
                if isamp:
                    zr = va2[:,iz,i,ipol]*np.cos(v2[:,iz,i,ipol])
                    zc = va2[:,iz,i,ipol]*np.sin(v2[:,iz,i,ipol])
                    z2 = zr + 1j*zc
                    z = scipy.interpolate.griddata(t2,z2,t1,method='linear')
                    h1['sol000/amplitude000/val'][:,iz,i,ipol] = abs(z)
                    h1['sol000/phase/val'][:,iz,i,ipol] = np.arctan2(z.imag,z.real)
                else:
                    z = scipy.interpolate.griddata(t2,\
                        np.unwrap(v2[:,iz,i,ipol]),t1,method='linear')
                    while z.max()>np.pi:
                        np.putmask(z,z>np.pi,z-2.*np.pi)
                    while z.min()<-np.pi:
                        np.putmask(z,z<-np.pi,z+2.*np.pi)
                    h1['sol000/phase000/val'][:,iz,i,ipol] = z
    h1.close(); h2.close()
    h1 = h5py.File(H1,'r+')
    n1 = h1.get('sol000/phase000')
    v1 = np.array(n1['val'])
    loop3log (vis,'\nInterpolation result:')
    for i in v1[0,0,:,0]:
        loop3log (vis,('%6.2f'%i) if ~np.isnan(i) else 'nan', cret=False)
    h1.close()

def calib (vis,incol='DATA',outcol='DATA',solint=180,solmode='P',\
           model=None,outms='.',outcal=None,tsamp=8.0,nchan=0):
    outcal = vis+'_cal' if outcal==None else outcal
    mgain = 'sourcedb=%s\n'%model if model else 'usemodelcolumn=true\n'
    caltype = 'phaseonly' if solmode=='P' else 'diagonal'
    f=open('calib.parset','w')
    f.write('msin=%s\n'%vis)
    f.write('msin.datacolumn=%s\n'%incol)
    f.write('msout=%s\n'%outms)
    f.write('msout.datacolumn=%s\n'%outcol)
    f.write('steps=[gaincal]\n')
    f.write('gaincal.'+mgain)
    f.write('gaincal.caltype=%s\n'%caltype)
    f.write('gaincal.solint=%i\n'%(solint/tsamp))
    f.write('gaincal.nchan=%i\n'%nchan)
    f.write('gaincal.usebeammodel=False\n')
    f.write('gaincal.parmdb=%s\n'%outcal)
    f.write('gaincal.applysolution=%s\n'%('False' if incol==outcol else 'True'))
    f.close()
    time_start = time.time()
    # Bug fix here: NDPPP leaves the .h5 files unclosed. So we have to 
    # start a separate python session to run the NDPPP on calib.parset, 
    # which closes the .h5 files on exit.
    fo=open('calib.py','w')
    fo.write ('import os\nos.system(\'NDPPP calib.parset\')\n')
    fo.close()
    os.system('python calib.py')
    time_end = time.time()
    loop3log(vis,'NDPPP took %d s' % int(time_end-time_start))

def coherence_metric (htab='1327_test.ms_cal.h5',antenna_list='',solset='sol000',soltab='phase000'):
    # Make the coherence parameter. This relies on the difference in the phase
    # solutions in XX and YY remaining constant if the solutions are coherent.
    # Also need to return an incoherent answer (2.0) if there are too many NaN
    # solutions (here >10%)
    NANFRAC, INCOH = 0.1, 2.0
    v, vm = h5read (htab, solset, soltab)
    ant,freq,pol,time = vm['ant'],vm['freq'],vm['pol'],vm['time']
    if type(ant) != list:
	ant = ant.tolist()
    coh = np.array([])
#   If no antenna list is passed, assume that the antennas in the calibration
#   table are the ones requested. This will fail on return if they are not
#   the same antennas in the same order.
    if not len(antenna_list):
        antenna_list=ant    
#   loop over antennas for which calibration is requested
    for i in range(len(antenna_list)):
        try: # find index of corresponding correction in the h5 
            j = ant.index(antenna_list[i])
# -- njj: do not use np.unwrap here - gives array full of NaN if even the
#    first element is NaN
            diff = v[:,0,j,0]-v[:,0,j,1]
            if float(len(diff[np.isnan(diff)]))>NANFRAC*float(len(diff)):
                coh = np.append(coh,INCOH)
            else:
                diff = np.unwrap(diff[~np.isnan(diff)])
                coh = np.append(coh,np.nanmean(np.gradient(abs(diff))**2))
        except:  # does not contain correction for this antenna
            coh = np.append(coh,INCOH)
    return coh

def snplt (vis,htab='1327_test.ms_cal.h5',solset='sol000',soltab='phase000',\
           antenna=None,nplot=6,outpng=None):
    outpng = outpng if outpng else htab
    v,vm = h5read (htab, solset, soltab)
    ant,freq,pol,time = vm['ant'],vm['freq'],vm['pol'],vm['time']
    time = 24.*(time/86400. - int(time[0])/86400)
    iplot = 0
    antenna = antenna if antenna else ant
    plt.clf()
    while iplot<len(antenna):
        a = antenna[iplot]
        aidx = np.argwhere(ant==a)[0][0]
        sys.stdout.write(a+' ')
        for ipol in range(v.shape[3]):
            if not (iplot+1)%nplot:
                plt.subplot(nplot,1,1+iplot%nplot)
            else:
                plt.subplot(nplot,1,1+iplot%nplot,xticks=[])
            if soltab[:5]=='phase':
                plt.plot(time,np.rad2deg(v[:,0,aidx,ipol]),'+')
                try:
                    plt.ylim(-180.,180.);plt.xlim(time[0],time[-1])
                except:
                    print 'Could not set one of the x axis limits. One of these is not real:', time[0], time[-1]
                    print 'Hard-coding to plt.xlim(0, 600)'
                    plt.ylim(-180.,180.);plt.xlim(0, 600)
                plt.text(time[0],180.0-12.*nplot,a)
            else:
                plt.plot(time,v[:,0,aidx,ipol],'+')
                vmin,vmax = min(v[:,0,aidx,ipol]),max(v[:,0,aidx,ipol])
                try:
                    plt.ylim(vmin,vmax);plt.xlim(time[0],time[-1])
                except:
                    print 'Could not set one of the axes limits. One of these is not real:', vmin, vmax, time[0], time[-1]
                plt.text(time[0],vmin+0.9*(vmax-vmin),a)
            plt.subplots_adjust(wspace=0,hspace=0)
        iplot+=1
        if not iplot%nplot:
            thispng = outpng+'_%d.png'%(iplot//nplot -1)
            if os.path.isfile(thispng):
                os.system('rm %s'%thispng)
            loop3log(vis,'-> %s'%thispng)
            try:
                plt.savefig(thispng) # ,bbox_inches='tight')
            except:
                print 'Failed to save', thispng
            plt.clf()
    if iplot%nplot:
        thispng = outpng+'_%d.png'%(iplot//nplot)
        if os.path.isfile(thispng):
            os.system('rm %s'%thispng)
        loop3log(vis,'-> %s'%thispng)
        try:
            plt.savefig(thispng) #,bbox_inches='tight')
        except:
            print 'Failed to save', thispng

def imagr (vis,threads=0,mem=100,doupdatemodel=True,tempdir='',dosaveweights=False,doprimary=False,\
           robust=-1,domfsweight=False,gausstaper=0.0,tukeytaper=0.0,dostoreweights=False,outname='wsclean',\
           imsize=1024,cellsize='0.05asec',dopredict=False,niter=10000,pol='I',datacolumn='',autothreshold=3.,\
	   dolocalrms=False,gain=0.1,mgain=1.0,domultiscale=False,dojoinchannels=False,channelsout=0,fitsmask='',\
	   baselineaveraging=0.0,maxuvwm=0.0,minuvwm=0.0,maxuvl=0.0,minuvl=0.0,dostopnegative=False,automask=0.,\
	   dosavesourcelist=False,weightingrankfilter=0.0,weightingrankfiltersize=0.0):
    cmd = 'wsclean '
    cmd += ('' if not threads else '-j '+str(threads)+' ')
    cmd += ('' if not mem==100 else '-mem '+str(mem)+' ')
    cmd += ('' if doupdatemodel else '-no-update-model-required ')
    cmd += tempdir+' '
    cmd += ('' if not dosaveweights else '-save-weights ')
    cmd += ('' if not doprimary else '-apply-primary-beam ')
    if robust >=5:
	cmd += '-weight natural '
    elif robust <=-5:
	cmd += '-weight uniform '
    else:
	cmd += '-weight briggs %f '%robust
    cmd += ('' if not domfsweight else '-mfs-weighting ')
    cmd += ('' if gausstaper==0.0 else '-taper-gaussian %f '%gausstaper)
    cmd += ('' if tukeytaper==0.0 else '-taper-tukey %f '%tukeytaper)
    cmd += ('' if not dostoreweights else '-store-imaging-weights ')
    cmd += '-name '+outname+' '
    cmd += '-size '+str(imsize)+' '+str(imsize)+' '
    cmd += '-scale '+str(cellsize)+' '
    cmd += ('' if not dopredict else '-predict ')
    cmd += ('-niter '+str(niter)+' ')
    cmd += ('' if pol=='I' else '-pol '+pol+' ')
    cmd += ('' if datacolumn=='' else '-datacolumn %s '%datacolumn)
    cmd += ('' if autothreshold==0. else '-auto-threshold %f '%autothreshold)
    cmd += ('' if not dolocalrms else '-local-rms ')
    cmd += ('' if not domultiscale else '-multiscale ')
    cmd += ('' if channelsout==0 else '-channels-out %d -join-channels '%channelsout )    
    cmd += ('' if gain==0.1 else '-gain %f '%gain)
    cmd += ('' if mgain==1.0 else '-mgain %f '%mgain)
    cmd += ('' if fitsmask=='' else '-fits-mask %s '%fitsmask)
    cmd += ('' if baselineaveraging==0.0 else '-baseline-averaging %f '%baselineaveraging)
    cmd += ('' if maxuvl==0.0 else '-maxuv-l %f '%maxuvl)
    cmd += ('' if minuvl==0.0 else '-minuv-l %f '%minuvl)
    cmd += ('' if maxuvwm==0.0 else '-maxuvw-m %f '%maxuvwm)
    cmd += ('' if minuvwm==0.0 else '-minuvw-m %f '%minuvwm)
    cmd += ('' if not dostopnegative else '-stop-negative ')
    cmd += ('' if automask==0. else '-auto-mask %f '%automask)
    cmd += ('' if not dosavesourcelist else '-save-source-list ')
    cmd += ('' if weightingrankfilter==0.0 else '-weighting-rank-filter %f '%weightingrankfilter)
    cmd += ('' if weightingrankfiltersize==0.0 else '-weighting-rank-filter-size %f '%weightingrankfiltersize)
    cmd += vis+ '>>wsclean_chunterings'
    loop3log (vis,'Executing: '+cmd)
    os.system (cmd)

def getcoh_baseline (antenna_list, coh, ccrit):
    '''Returns the maximum baseline length for imaging given
    a list of coherences for stations'''
    aname = ['DE601','DE602','DE603','DE604','DE605','DE609','SE','FR',\
	     'UK','PL','IE']
    alen = [260,580,400,420,230,200,600,700,602,800,800]
    cohlength = 2000.0
    np.putmask(coh,coh==-1.0,ccrit)
    for i in range(len(antenna_list)):
	for j in range(len(aname)):
	    if antenna_list[i][:len(aname[j])]==aname[j]:
		if coh[i]>ccrit-0.1:
		    cohlength = alen[j]
		    break
    return 1000.0*cohlength


#### for plotting a montage at the end
def sort_filelist( myfiles ):
    ## sort the files by self-cal iteration
    file_index = []
    for myfile in myfiles:
	tmp = myfile.split('_')[-1]
	myloop = tmp.split('-')[0]
	if myloop == 'final':
	    myloop = '999'
	file_index.append(np.int(myloop))
    sort_index = np.argsort(file_index)
    file_array = np.array(myfiles)
    myfiles = list(file_array[sort_index])
    return myfiles    

def plot_im( fitsfile, max_scaling=1.0, figsize=3, rms=0., rms_scaling=3. ):
    hdu = pyfits.open(fitsfile)[0]
    wcs = WCS(hdu.header,naxis=2)
    if rms == 0:
	rms = np.std(hdu.data)
    if hdu.data.max()*max_scaling < rms:
	print( 'Max value is less than the rms, setting max value scaling to 1' )
	max_scaling = 1.
    fig = plt.figure()
    ax = fig.add_subplot(1,1,1,projection=wcs)
    ax.imshow(hdu.data[0,0,:,:], vmin=rms*rms_scaling, vmax=hdu.data.max()*max_scaling)
    ax.annotate('rms=%.5f Jy/bm'%rms,(0.25,0.8),xycoords="figure fraction", color="white", weight="bold", size="large" )
    ax.set_title(fitsfile.replace('.fits',''), fontsize=figsize*2.75)
    fig.savefig( fitsfile.replace('.fits','.pdf') )

def make_montage( filelist, outname='', nup='4x2' ):
    # create the filename 
    tmp = filelist[0].split('_')
    if 'image' in filelist[0]:
	imtype = 'image'
    elif 'residual' in filelist[0]:
	imtype = 'residual'
    outname = tmp[0] + '_' + imtype + '.pdf'
    # move them to a temporary directory for making a montage
    os.system('mkdir montage_tmp')
    imlist = []
    for myfile in filelist:
	myim = myfile.replace('.fits','.pdf')
	imlist.append( myim )
	os.system( 'mv %s montage_tmp/'%myim )
    # montage them together 
    os.chdir('montage_tmp')
    os.system('montage *.pdf -tile %s -geometry 600x600 %s'%(nup,outname))
    os.system('mv %s ../'%outname )
    os.chdir('../')
    os.system('rm -r montage_tmp' )

def montage_plot( filepattern, imscale=0.65, nup='4x2', plot_resid=True):
    if filepattern.split('.')[-1] == 'fits':
	filelist = sort_filelist(glob.glob(filepattern))
    else:
	filelist = sort_filelist(glob.glob('%s*-MFS-image.fits'%filepattern))
    # open the final image to get scaling parameters
    final_im = filelist[-1]
    with pyfits.open( final_im ) as im_hdu:
	data = im_hdu[0].data
    im_hdu.close()
    std_estimate = np.abs(np.median(data))
    max_val = np.max(data)*imscale
    # plot the images on the same scale
    for myfile in filelist:
	# get the std dev of the residual image
	res_im = myfile.replace('image','residual')
	with pyfits.open( res_im ) as im_hdu:
	    data = im_hdu[0].data
	im_hdu.close()
	res_vals = np.std(data)
	plot_im( myfile, max_scaling = imscale*max_val, rms = res_vals, rms_scaling=1 )
	if plot_resid:
	    plot_im( myfile.replace('image','residual'), max_scaling = 0.1, rms = res_vals, rms_scaling=1 )
    # make a montage
    make_montage( filelist, nup=nup )
    if plot_resid:
	residlist = []
	for myfile in filelist:
	    residlist.append(myfile.replace('image','residual'))
	make_montage( residlist, nup=nup )

def cleanup(vis):
    os.system('rm -fr %s_processing'%vis)
    os.system('mkdir %s_processing'%vis)
    h5all = np.sort(glob.glob(vis+'*_*c0.h5'))
    h5A = np.sort(glob.glob(vis+'*A_*c0.h5'))
    h5vis = h5all[~np.in1d(h5all,h5A)]
    os.system('mv %s*.fits %s_processing'%(vis,vis))
    os.system('mv %s*.log %s_processing'%(vis,vis))
    os.system('mv %s*.png %s_processing'%(vis,vis))
    os.system('mv %s*.h5 %s_processing'%(vis,vis))
    calfiles = np.array([])
    if len(h5vis):
	h5vis_head, h5vis_tail = os.path.split(h5vis[-1])  # Sean added
	os.system('cp %s_processing/%s .'%(vis,h5vis_tail))  # Sean edited
	calfiles = np.append(calfiles,h5vis[-1])
    if len(h5A):
	h5A_head, h5A_tail = os.path.split(h5A[-1])  # Sean added
	os.system('cp %s_processing/%s .'%(vis,h5A_tail))  # Sean edited
	calfiles = np.append(calfiles,h5A[-1])
    vis_head, vis_tail = os.path.split(vis)
    os.system('cp %s_processing/%s_output.png .'%(vis,vis_tail))

    return '%s_output.png'%vis, calfiles

def imaging(vis,niters,threshold,minuvw,robust):
    imagr (vis,cellsize='0.05asec',imsize=1024,maxuvl=1000000,robust=robust,\
	  minuvwm=minuvw,gain=0.1,mgain=0.85,dostopnegative=True,niter=niters,\
	  autothreshold=threshold,weightingrankfiltersize=256,\
	  weightingrankfilter=3,domultiscale=True,automask=7.0,\
	  outname='test',dolocalrms=True)
# Needs an initial model. May be provided as:
#    model=None        Make an image and use that. (Unlikely to be a good idea)
#    model='MODEL'     Look in the MODEL_DATA column
#    model=[filename]  LOFAR sourcedb format e.g. converted from FIRST, LoTSS, EHT imager....
#                      [Not working yet, maybe require calling routine to do this?]
#  NOTE: uses bdsf - version 1.8.13 which loads by default has a conflict with
#  other libraries - may need to unload and use 1.8.10 instead 

def selfcal(vis,minuvw,robust,model='MODEL',outcal_root='',max_sol=600.0,init_sol=30.0,\
	    incol='DATA',outcol='DATA',caltype='P',nchan=0):
    if not model:
	imaging(vis,1000,10,minuvw,robust)
    # need a predict step to deal with sourcedb here if necessary
    ptant = casatb.table(vis+'/ANTENNA')
    antenna_list = np.array([],dtype='S')
    for i in ptant.select('NAME'):
	antenna_list = np.append(antenna_list,i.values())
    nant = len(antenna_list)
    if caltype=='P':
	sol_int_range = np.arange(np.ceil(np.log(max_sol/init_sol)/np.log(3.)))
	sol_int_range = np.ceil(init_sol*3.**sol_int_range)
	nsol = len(sol_int_range)
	coh = CCRIT*np.ones((nsol,nant))
	for i in range(nsol):
	    solint = sol_int_range[i]
	    outcal_root = outcal_root if len(outcal_root) else vis
	    outcal = outcal_root+'_c%d.h5'%i
	    loop3log (vis,'\n--- Beginning pass with solint %.1f sec ---' % (solint))
	    calib (vis, solint=solint, outcal=outcal, incol=incol, \
				 outcol=outcol,solmode='P',tsamp=TSAMP,nchan=nchan)
	    snplt (vis,htab=outcal,outpng=outcal)
	    coh[i] = coherence_metric (outcal,antenna_list)
	    loop3log(vis,'\nCoherences by antenna:')
	    for j in range(nant):
		loop3log(vis,'%.2f '%(coh[i,j]),cret=not((j+1)%10))
            loop3log(vis,' ')
	    if len(coh[i][coh[i]>=CCRIT])==0:  # all coherent
		break

    # For each antenna in the antenna list, find the selfcal table with 
    # the shortest solution interval that contains coherent solutions. If 
    # there is no such table, report -1 in order to signal that they should 
    # all be set to zero.
	ncoh = np.ones(nant,dtype=int)*-1
	allcoh = np.ones(nant,dtype=float)*CCRIT
	for i in range(nant):
	    try:
		ncoh[i] = np.min(np.ravel(np.argwhere(coh[:,i]<CCRIT)))
		allcoh[i] = coh[:,i][ncoh[i]]
	    except:
		pass
        loop3log(vis,'\nCombined coherences: ')
        for i in range(nant):
    	    loop3log(vis,'%.2f '%(allcoh[i]),cret=not((i+1)%10))
        loop3log(vis,'\nSolution number with first coherence (-1=no coh):')
        for i in range(nant):
    	    loop3log(vis,'%d '%(ncoh[i]),cret=not((i+1)%10))
        loop3log(vis,' ')
        loop3log(vis,' ----- Starting edit of this solution ------')
    # For each selfcal table containing the shortest solution interval with 
    # coherence on some antennas, replace the entries in the first selfcal 
    # table with the interpolated values from that antenna
	for i in range(1,coh.shape[0]):
	    iant = antenna_list[ncoh==i]
            loop3log(vis,'Editing %d antennas to h5parm number %d'%(len(iant),i))
	    if len(iant):
		clcal (vis,outcal_root+'_c0.h5',outcal_root+'_c%d.h5'%i,\
				     ant_interp=iant)
    # For each antenna without any coherence at all, zero the phase 
    # solutions for that antenna
	iant = antenna_list[ncoh==-1]
	if len(iant):
            loop3log (vis,'Trying to zero antennas: ',cret=False)
            for i in range(len(iant)):
                loop3log(vis,'%s '%iant[i],cret=False)
            loop3log(vis,'\n')
	    zerosol (vis,outcal_root+'_c0.h5',iant)
    else:    # amplitude selfcal: only one interval
	outcal_root = outcal_root if len(outcal_root) else vis
	outcal = outcal_root+'_c0.h5'%i
	calib (vis, solint=init_sol, outcal=outcal, incol=incol, \
				 outcol=outcol,solmode='A', nchan=nchan)
	snplt (vis,htab=outcal,outpng=outcal,soltab='amplitude000')
	allcoh = coherence_metric (outcal,antenna_list)
        loop3log(vis,'Coherences: \n')
        for i in range(nant):
    	    loop3log(vis,'%.2f '%(allcoh[i]),cret=not((i+1)%10))
        loop3log(vis,' ')
    # For each antenna without any coherence at all, zero the amp/phase 
    # solutions for that antenna
    # corrected bug here (Neal) - used to be < so everything was zeroed
	iant = antenna_list[allcoh>=CCRIT]
        print ('******',allcoh)
        print ('******>>> ',iant)
	if len(iant):
	    zerosol (vis,outcal_root+'_c0.h5',ant=iant)
    # find the maximum baseline length with coherent cal signal
    cohlength = getcoh_baseline (antenna_list,allcoh,CCRIT)
    return allcoh,cohlength

# following is based on Frits's algorithm with measure_statistic

def measure_statistic2 (filename):
    img = pyfits.open(filename)[0].data.squeeze()
    im_rms = np.std(img)
    im_max = np.max(img)
    resfile = filename.replace('image','residual')
    res = pyfits.open(resfile)[0].data.squeeze()
    res_rms = np.std(res)
    res_max = np.max(res)
    snr = im_max / res_rms
    return snr

def measure_statistic ( filename ):
    img = pyfits.open(filename)[0].data.squeeze()
    return abs (img.max()/img.min())

def applycal_split (vis, visA, solset, parmdb, soltab='phase000',\
		    correction='phase000'):
    fo = open('applycal.parset','w')
    fo.write ('msin=%s\n'%vis)
    fo.write ('msout=%s\n'%vis)
    fo.write ('msin.datacolumn=DATA\n')
    fo.write ('msout.datacolumn=CORRECTED_DATA\n')
    fo.write ('steps=[applycal]\n')
    fo.write ('applycal.type=applycal\n')
    fo.write ('applycal.parmdb=%s\n'%parmdb)
    fo.write ('applycal.solset=%s\n'%solset)
    fo.write ('applycal.soltab=%s\n'%soltab)
    fo.write ('applycal.correction=%s\n'%correction)
    fo.close()
    os.system ('NDPPP applycal.parset')
    fo = open('split.parset', 'w')
    fo.write ('msin=%s\n'%vis)
    fo.write ('msin.datacolumn=CORRECTED_DATA\n')
    fo.write ('msout=%s\n'%visA)
    fo.write ('msout.datacolumn=DATA\n')
    fo.write ('steps=[]\n')
    fo.close()
    os.system ('NDPPP split.parset')

def fits_axzapit (h, k):
    try:
        h.remove(k)
        return False
    except:
        return True

def fits_axzap (infile,outfile):
    hdulist = pyfits.open(infile)
    hdu = hdulist[0]
    while hdu.data.ndim>2:
        shrinkax = np.argwhere(hdu.data.shape==np.min(hdu.data.shape))[0][0]
        hdu.data = np.take (hdu.data,0,axis=shrinkax)
    i=2
    while True:
        i+=1
        isit = fits_axzapit (hdu.header,'CRPIX%d'%i)
        fits_axzapit (hdu.header,'CTYPE%d'%i)
        fits_axzapit (hdu.header,'CUNIT%d'%i)
        fits_axzapit (hdu.header,'CRVAL%d'%i)
        fits_axzapit (hdu.header,'CDELT%d'%i)
        fits_axzapit (hdu.header,'CRPIX%d'%i)
        fits_axzapit (hdu.header,'CROTA%d'%i)
        hdu.header.update()
        if isit:
            break
    os.system ('rm '+outfile)
    hdu.writeto (outfile)

def make_plots(vis):
    import glob
    imgroot = np.sort(glob.glob(vis+'*MFS-image.fits'))
    nloop = len(imgroot)
    h5png = glob.glob(vis+'*h5_*.png')
    npng = 0
    for i in h5png:
        npng = max(npng,int(i.split('_')[-1].split('.')[0])+1)
    cmd = 'montage -tile %dx%d -geometry 600x600 '%(npng+1,nloop)
    for i in range(nloop):
        aplpy_plots( imgroot[i] )
        thisv = imgroot[i].split('-MFS-image.fits')[0]
        for j in range(npng):
            this = '%s_c0.h5_%d.png'%(thisv,j)
            cmd += (this+' ') if os.path.isfile(this) else 'null: '
        this = thisv+'-MFS-image.png'
#        print 'trying to add',this,os.path.isfile(this)
        cmd += (this+' ') if os.path.isfile(this) else 'null: '
    cmd += '%s_output.png'%vis
    print cmd
    os.system(cmd)

def aplpy_plots( infits, docut=2.0, outpng='', nolabel=False,  crms=3.0, noshift=False, margin=1.7 ):

    ## open the file to get the data and the header information
    print 'Plotting ',infits
    hdul = pyfits.open( infits )
    a = hdul[0].data.squeeze()
    h = hdul[0].header
    nx,ny = h['NAXIS1'],h['NAXIS2']
    field_radius = h['CDELT2']*ny/2.0
    trms,tmax = np.array([]), np.array([])
    for i in range(10):
        x1,x2 = int(0.1*i*nx),int(0.1*(i+1)*nx-1)
        for j in range(10):
            y1,y2 = int(0.1*j*ny),int(0.1*(j+1)*ny-1)
            trms = np.append(trms,np.std(a[y1:y2,x1:x2]))
            tmax = np.append(tmax,np.std(a[y1:y2,x1:x2]))
    rms = np.nanmedian(trms)
    vmin,vmax = np.nanmin(a),np.nanmax(a)

    ## calculate the required sigma
    reqsig = stats.norm.ppf(1-0.5/float(nx*ny)) + (2.0 if docut<-1.0 else docut)

    ## run bdsf to get a gaussian list
    img = bdsf.process_image( infits, thresh_isl=reqsig )
    img.write_catalog( outfile='temp.gaul', catalog_type='gaul', format='ascii', clobber=True )

    ## read in the file
    s = ascii.read('temp.gaul',format='commented_header', header_start=4)
    print s
   
    ## make a plot
    gc = aplpy.FITSFigure(infits)
    pixra,pixdec = np.mean(s['Xposn']),np.mean(s['Yposn'])
    dec = h['CRVAL2']+h['CDELT2']*(pixdec-h['CRPIX2'])
    cosdec = np.cos(np.deg2rad(dec))
    ra = h['CRVAL1']+h['CDELT1']*(pixra-h['CRPIX1'])/cosdec
    deccen = h['CRVAL2']+h['CDELT2']*(0.5*ny-h['CRPIX2'])
    cosdeccen = np.cos(np.deg2rad(deccen))
    racen = h['CRVAL1']+h['CDELT1']*(0.5*nx-h['CRPIX1'])/cosdeccen
    pix_range = max(s['Xposn'].max()-s['Xposn'].min(), s['Yposn'].max()-s['Yposn'].min())
    try:
        deg_range = max(margin*h['CDELT2']*pix_range,0.1*ny*h['CDELT2'])
    except:
        deg_range = 1./60.
        print 'Error on deg_range, pix_range %f'%pix_range
    if docut<-1.0:
        deg_range = -2.0*docut/3600.
    print 'Range is %.1f arcsec, %.1f pix\n'%(1800.0*deg_range,pix_range)
    if noshift:
        gc.recenter(racen,deccen,0.5*deg_range)
    else:
        gc.recenter(ra,dec,0.5*deg_range)
    gc.set_tick_color('black')
    gc.show_colorscale(cmap=matplotlib.cm.gray_r,vmin=vmin,vmax=vmax)
    levels, tlevels = [vmax], vmax
    while tlevels > crms*rms:
        tlevels /= np.sqrt(2)
        levels.append(tlevels)
    gc.show_contour(levels=np.sort(levels))
    if not nolabel:
        bstr=''
        for i in range(len(h)):
            try:
                if 'BMAJ' in h[i] and 'AIPS' in h[i]:
                    bmaj = 3600.*float(h[i].split('BMAJ=')[1].split()[0])
                    bmin = 3600.*float(h[i].split('BMIN=')[1].split()[0])
                    bstr = 'beam %.1fx%.1f'%(bmaj,bmin)
            except:
                pass
        if rms>0.1:
            gc.add_label(0.5,0.05,'Peak %.1f, rms %.1f Jy %s'%\
                    (vmax,rms,bstr),relative=True,size=14)
        elif rms>1.e-4:
            gc.add_label(0.5,0.05,'Peak %.1f, rms %.1f mJy %s'%\
                    (vmax*1000.,rms*1000.,bstr),relative=True,size=14)
        else:
            gc.add_label(0.5,0.05,'Peak %.1f, rms %.1f uJy %s'%\
                    (vmax*1.e6,rms*1.e6,bstr),relative=True,size=14)
    if outpng=='':
        outpng=infits.replace('fits','png')
    gc.save(outpng)
    os.system('rm default.conv;rm default.sex;rm default.param;rm temp.fits')


def main (vis,strategy='P30,P30,P30,A500,A450,A400',startmod='',ith=5.0,\
          bandwidth='8MHz',goodness=2.,minuvw=50.0,robust=-1.0):
    ## format arguments
    strategy = str(strategy)
    startmod = str(startmod)
    ith = float(ith)
    bandwidth = str(bandwidth)
    minuvw = float(minuvw)*1000.0   # convert km -> m
    robust = float(robust)
    ## process arguments
    vis = vis.rstrip('/')
    vis = vis.split('/')[-1]
    strategy = strategy.split(',')
    bw_val = ''
    bw_unit = ''
    for c in bandwidth:
	try:
	    float(c)
	    bw_val = bw_val + c
	except ValueError:
	    bw_unit = bw_unit + c
    if bw_unit == 'MHz':
	bw_val = float(bw_val)*1e6
    ## get bandwidth of vis
    spec_info = casatb.table( vis + '::SPECTRAL_WINDOW')
    total_bw = spec_info.getcol('TOTAL_BANDWIDTH')[0]
    num_chan = spec_info.getcol('NUM_CHAN')[0]
    spec_info.close()
    if total_bw < bw_val:
	wsclean_chans = 0
	mfs = ''
	nchan=0
    else:
	wsclean_chans = int( np.ceil(total_bw/bw_val) )
	mfs='-MFS'
	nchan=num_chan/wsclean_chans
## make a working directory and go there
    tmp_dir = 'loop3_'+vis.rstrip('.ms').rstrip('.MS')
    os.system('mkdir %s'%tmp_dir)
    os.chdir(tmp_dir)
    os.system('mv ../%s .'%vis)
    import bdsf
    prevstat = 0.0
    cohlength = 2.0E6
    strategy_type = []
    for i in strategy: 
        strategy_type.append(i[0])
    ploop, nloop, snver = strategy_type.count('P'), len(strategy), 0
    #
    # PHASE CALIBRATION - run through ploop iterations, exiting if we have convergence
    #
    for iloop in range(ploop):
        fitsmask = vis+'_%02d-mask.fits'%(iloop-1) if iloop else ''
        if startmod=='' or iloop:
            pstr = '******* PHASE LOOP %d running wsclean ************'%iloop
            loop3log (vis, pstr+'\n')
            imagr(vis,minuvwm=minuvw,robust=robust,cellsize='0.05asec',domultiscale=True,\
                  outname=vis+'_%02d'%iloop,channelsout=wsclean_chans,\
                  fitsmask=fitsmask,dolocalrms=True,maxuvwm=cohlength)
        else:
            # Need something here to produce an image from startmod
            pass
        # check if there's a source
        thisstat = measure_statistic2(vis+'_%02d%s-image.fits'%(iloop,mfs))
        if thisstat < goodness:
            pstr = 'SNR is %f, breaking out of loop.'%thisstat
            loop3log( vis, pstr+'\n' )
	    montage_plot( '*MFS-image.fits', imscale=0.65, nup='4x2', plot_resid=False)
            return(0)
        pstr='******* PHASE LOOP %d making mask %s_%02d%s-image.fits ********'%(iloop,vis,iloop,mfs)
        loop3log (vis, pstr+'\n')
        stdout = sys.stdout; sys.stdout = open('bdsf_chunterings','a')
        img=bdsf.process_image('%s_%02d%s-image.fits'%(vis,iloop,mfs),atrous_do=True,thresh_isl=ith)
        sys.stdout.close(); sys.stdout = stdout
        img.export_image(img_type='island_mask',outfile='%s_%02d-mask.fits'%(vis,iloop))
        # exit loop if clean finishing
        pstr='******* PHASE LOOP %d goodness stat %f ************' % (iloop,thisstat)
        loop3log (vis, pstr+'\n')
        if thisstat-prevstat<0.01:
            pstr='****** EXITING PHASE CAL with diff %f *********'%(thisstat-prevstat)
            loop3log (vis, pstr+'\n')
            break
        else:   
            prevstat = thisstat
            imagr(vis,minuvwm=minuvw,robust=robust,dopredict=True,fitsmask=fitsmask,\
                  autothreshold=3,dolocalrms=True,\
                  outname=vis+'_%02d%s'%(iloop,mfs))
        pstr='******* PHASE LOOP %d making new cal file %s ************' % (iloop,vis+'_%02d'%iloop)
        loop3log (vis, pstr+'\n')
        caltype, sol0 = strategy[iloop][0], float(strategy[iloop][1:])
        coh, cohlength = selfcal(vis,minuvw,robust,model='MODEL',incol='DATA',\
            outcol='CORRECTED_DATA',outcal_root=vis+'_%02d'%iloop,\
            caltype=caltype,init_sol=sol0,nchan=nchan)
        snver = iloop
        pstr='******** END PHASE LOOP %d - coherence on %.1f km **********' % \
              (iloop,cohlength/1000.)
        loop3log (vis, pstr+'\n')
    # Exit at this point if we are not doing amplitude cal
    if ploop == nloop:
        exit()
    #
    # If we are doing amplitude calibration, we now need to apply the 
    # calibration and write a new MS with a DATA column
    visA = vis+'_A'
    # delete all existing files beginning with vis+'_A'
    os.system('rm -fr %s*'%visA)
    pstr='****** APPLYING CALIBRATION TABLE %d\n'%snver
    loop3log (vis, pstr+'\n')
    applycal_split (vis, visA, 'sol000', '%s_%02d_c0.h5' % (vis,snver))
    init_fitsmask = vis+'_%02d-mask.fits'%iloop
    init_img = vis+'_%02d%s-image.fits'%(iloop,mfs)
    pred_img = vis+'_%02d%s'%(iloop,mfs)
    for iloop in range(ploop,nloop):
        fitsmask = init_fitsmask if iloop==ploop else visA+'_%02d-mask.fits'%(iloop-1)
        pstr='******* AMPLITUDE LOOP %d running wsclean ************'%iloop
        loop3log (vis, pstr+'\n')
        imagr(visA,minuvwm=minuvw,robust=robust,cellsize='0.05asec',domultiscale=True,\
                  outname=visA+'_%02d'%iloop,channelsout=wsclean_chans,\
                  fitsmask=fitsmask,dolocalrms=True,maxuvwm=cohlength)
	## check if there's a source
        thisstat = measure_statistic2(visA+'_%02d%s-image.fits'%(iloop,mfs))
        if thisstat < goodness:
            pstr = 'SNR is %f, breaking out of loop.'%thisstat
            loop3log( vis, pstr+'\n' )
	    montage_plot( '*MFS-image.fits', imscale=0.65, nup='4x2', plot_resid=True)
            return(0)
        image_bdsf = '%s_%02d%s-image.fits'%(visA,iloop,mfs)
        pstr='******* AMPLITUDE LOOP %d making mask %s_%02d%s-image.fits ************'%(iloop,visA,iloop,mfs)
        loop3log (vis, pstr+'\n')
        img=bdsf.process_image(image_bdsf,atrous_do=True,thresh_isl=ith)
        img.export_image(img_type='island_mask',outfile='%s_%02d-mask.fits'%(visA,iloop))
        pstr='******* AMPLITUDE LOOP %d goodness stat %f ************' % (iloop,thisstat)
        loop3log (vis, pstr+'\n')
        if iloop!=ploop and thisstat-prevstat<0.01:
            pstr='****** EXITING AMPLITUDE CAL with diff %f *********'%(thisstat-prevstat)
            loop3log (vis, pstr+'\n')
	    break
        else:   
            prevstat = thisstat
            imagr(visA,minuvwm=minuvw,dopredict=True,fitsmask=fitsmask,\
                  autothreshold=3,dolocalrms=True,robust=robust,\
                  outname=visA+'_%02d%s'%(iloop,mfs))
        pstr='******* AMPLITUDE LOOP %d making new cal file %s ************' % (iloop,visA+'_%02d'%iloop)
        loop3log (vis, pstr+'\n')
        caltype, sol0 = strategy[iloop][0], float(strategy[iloop][1:])
        coh,cohlength = selfcal(visA,minuvw,robust,model='MODEL',incol='DATA',\
            outcol='CORRECTED_DATA',outcal_root=visA+'_%02d'%iloop,\
            caltype=caltype,init_sol=sol0,nchan=nchan)
        pstr='******** END AMPLITUDE LOOP %d - coherence on %.1f km **********' % \
                      (iloop,cohlength/1000.)
        loop3log (vis, pstr+'\n')


    fitsmask = init_fitsmask if iloop==ploop else visA+'_%02d-mask.fits'%(iloop-1)
    imagr(visA,minuvwm=minuvw,cellsize='0.05asec',domultiscale=True,\
          outname=visA+'_final',channelsout=wsclean_chans,robust=robust,\
          fitsmask=fitsmask,dolocalrms=True)

    ## make a model from the final image
    final_im = glob.glob('*final*image.fits')
    if len(final_im) > 1:
	tmp = [ a for a in final_im if 'MFS' in a ]
	final_im = tmp
    img = bdsf.process_image( final_im[0], atrous_do=True, thresh_isl=ith )
    skyfile = final_im[0].replace('fits','skymodel')
    img.write_catalog( outfile=skyfile, bbs_patches='single', catalog_type='gaul', format='bbs' )
    ## convert it to a sourcedb
    ss = "makesourcedb in=%s out=%s format='<'"%(skyfile,skyfile.replace('skymodel','sky'))
    os.system(ss)

    ## plot things like solutions
    make_plots( vis )
    make_plots( visA )

    ## If we got to this point, self-cal has successfully completed    
    montage_plot( '*MFS-image.fits', imscale=0.65, nup='4x2', plot_resid=True)

    pngfile, h5files = cleanup (vis)

    for h5file in h5files:
        os.system('mv %s ../'%h5file)
    os.system('mv *.pdf ../')
    os.system('mv *.png ../')
    os.system('mv *skymodel ../')
    os.system('mv *sky ../')
    os.system('mv %s ../'%vis )
    
    print 'Output calibration tables',h5files
    return pngfile,h5files

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('vis', type=str, help='measurement set')
    parser.add_argument('--strategy', default='P30,P30,P30,A500,A450,A400', type=str, help='strategy for loops (default:P30,P30,P30,A500,A450,A400)' )
    parser.add_argument('--startmod', default='', type=str, help='starting model')
    parser.add_argument('--ith', default=5.0, type=float, help='threshold for pybdsf island detection, default 5.0')
    parser.add_argument('--bandwidth', default='8MHz', type=str, help='max bandwidth before breaking imaging into channels, default 8MHz')
    parser.add_argument('--goodness', default=2.0, type=float, help='cutoff between noise and source' )
    parser.add_argument('--minuvw', default=50.0, type=float, help='minimum baseline in km' )
    parser.add_argument('--robust', default=-1, type=float, help='Briggs weighting' )


    args = parser.parse_args()

    main( vis=args.vis, strategy=args.strategy, ith=args.ith, bandwidth=args.bandwidth, goodness=args.goodness, minuvw=args.minuvw, robust = args.robust )

