import numpy as np
from astropy.io import fits
from astropy.table import Table, vstack
from scipy import optimize, stats
import argparse
import os

from ..lib.logllh_ebins_funcs import get_cnt_ebins_normed, log_pois_prob
from ..response.ray_trace_funcs import ray_trace_square
from ..lib.drm_funcs import get_ebin_ind_edges, DRMs
from ..lib.event2dpi_funcs import det2dpis, mask_detxy
from ..lib.trans_func import get_pb_absortion


def dpi2cnts_perquad(dpi):
    x_mid = 142
    y_mid = 86

    quads = []
    quads.append(np.sum(dpi[:y_mid, :x_mid]))
    quads.append(np.sum(dpi[y_mid:, :x_mid]))
    quads.append(np.sum(dpi[y_mid:, x_mid:]))
    quads.append(np.sum(dpi[:y_mid, x_mid:]))
    return quads


def ev2quad_cnts(ev):
    x_mid = 142
    y_mid = 86

    quads = [np.sum((ev["DETX"] < x_mid) & (ev["DETY"] < y_mid))]
    quads.append(np.sum((ev["DETX"] < x_mid) & (ev["DETY"] > y_mid)))
    quads.append(np.sum((ev["DETX"] > x_mid) & (ev["DETY"] > y_mid)))
    quads.append(np.sum((ev["DETX"] > x_mid) & (ev["DETY"] < y_mid)))
    return np.array(quads)


def dmask2ndets_perquad(dmask):
    quads = dpi2cnts_perquad((dmask == 0).reshape(dmask.shape))

    return quads


def quads2drm_imxy():
    # bottom left, top left, top right, bottom right
    quads_imxy = [(1.0, 0.5)(0.8, -0.4), (-0.75, -0.45), (-1.1, 0.5)]

    return quads_imxy


def halves2drm_imxy():
    # left, top, right, bottom
    halves_imxy = [(1.0, 0.15), (0.0, -0.5), (-1.0, 0.15), (0.0, 0.45)]

    return halves_imxy


def get_abs_cor_rates(imx, imy, drm):
    drm_emids = (drm[1].data["ENERG_LO"] + drm[1].data["ENERG_HI"]) / 2.0
    absorbs = get_pb_absortion(drm_emids, imx, imy)
    abs_cor = (2.0 - absorbs) / (absorbs)
    return abs_cor


def profiled_bkg_llh(
    data_cnts, sig_rate, sdt, off_cnts, odt, off_cnts_err, ret_f=False
):
    sig2 = off_cnts_err**2
    d_i = np.sqrt(
        (sdt * sig2 - odt * off_cnts + (odt**2) * sig_rate) ** 2
        - 4.0
        * (odt**2)
        * (sdt * sig2 * sig_rate - data_cnts * sig2 - odt * off_cnts * sig_rate)
    )

    f_i = (-(sdt * sig2 - odt * off_cnts + sig_rate * (odt**2)) + d_i) / (
        2.0 * odt**2
    )

    llh = (
        sdt * (sig_rate + f_i)
        - data_cnts * np.log(sdt * (sig_rate + f_i))
        + np.square(off_cnts - odt * f_i) / (2.0 * sig2)
        - data_cnts * (1.0 - np.log(data_cnts))
    )

    if ret_f:
        return np.sum(llh), f_i

    return np.sum(llh)


def rates_llh(data, nsig, sig_e_normed, bkg_cnts, bkg_err, sig_dt, bkg_dt, ret_f=False):
    sig_rates = (nsig / sig_dt) * sig_e_normed

    if ret_f:
        llh, f = profiled_bkg_llh(
            data, sig_rates, sig_dt, bkg_cnts, bkg_dt, bkg_err, ret_f=ret_f
        )

        return llh, f
    llh = profiled_bkg_llh(
        data, sig_rates, sig_dt, bkg_cnts, bkg_dt, bkg_err, ret_f=ret_f
    )

    return llh


def rate_llh2min(theta, data, bkg_cnts, bkg_err, sig_dt, bkg_dt, cnorm_obj):
    nsig = 10.0 ** theta[0]
    ind = theta[1]

    if (ind < -1) or (ind > 3):
        return np.inf

    cnt_ebn = cnorm_obj(ind)

    llh = rates_llh(data, nsig, cnt_ebn, bkg_cnts, bkg_err, sig_dt, bkg_dt)

    return llh


class cnts_norm_intp(object):
    def __init__(self, cnt_ebins_norm_ind_mat, ind_ax):
        self.ind_ax = ind_ax
        self.cnt_ebins_norm_ind_mat = cnt_ebins_norm_ind_mat
        self.ind0 = np.min(ind_ax)
        self.ind1 = np.max(ind_ax)

    def __call__(self, ind):
        if (ind <= self.ind0) or (ind >= self.ind1):
            return np.nan * np.ones(np.shape(self.cnt_ebins_norm_ind_mat)[1])

        ind_ind0 = np.argmin(np.abs(ind - self.ind_ax))
        ind_ind1 = ind_ind0 + 1 if ind > self.ind_ax[ind_ind0] else ind_ind0 - 1

        A0 = np.abs(ind - self.ind_ax[ind_ind1]) / np.abs(
            self.ind_ax[ind_ind0] - self.ind_ax[ind_ind1]
        )
        A1 = 1 - A0

        cnts_norm = (
            A0 * self.cnt_ebins_norm_ind_mat[ind_ind0]
            + A1 * self.cnt_ebins_norm_ind_mat[ind_ind1]
        )

        return cnts_norm


def get_cnts_intp_obj(ind_ax, drm, ebin_ind_edges, abs_cor):
    nebins = len(ebin_ind_edges)
    cnt_ebins_norm_ind_mat = np.zeros((len(ind_ax), nebins))

    for i in range(len(ind_ax)):
        cnt_ebins_norm_ind_mat[i] = get_cnt_ebins_normed(
            ind_ax[i], drm, ebin_ind_edges, abs_cor=abs_cor
        )

    intp_obj = cnts_norm_intp(cnt_ebins_norm_ind_mat, ind_ax)
    return intp_obj


def get_cnts_per_tbins(t_bins0, t_bins1, ebins0, ebins1, ev_data, dmask):
    ntbins = len(t_bins0)
    nebins = len(ebins0)
    cnts_per_tbin = np.zeros((ntbins, nebins))

    for i in range(ntbins):
        sig_bl = (ev_data["TIME"] >= t_bins0[i]) & (ev_data["TIME"] < (t_bins1[i]))
        sig_data = ev_data[sig_bl]

        sig_data_dpis = det2dpis(sig_data, ebins0, ebins1)
        cnts_per_tbin[i] = np.array(
            [np.sum(dpi[(dmask == 0)]) for dpi in sig_data_dpis]
        )

    return cnts_per_tbin


def get_quad_cnts_tbins(tbins0, tbins1, ebins0, ebins1, evd):
    ntbins = len(tbins0)
    nebins = len(ebins0)

    cnts_mat = np.zeros((ntbins, nebins, 4))

    for i in range(ntbins):
        sig_bl = (evd["TIME"] >= tbins0[i]) & (evd["TIME"] < (tbins1[i]))
        sig_data = evd[sig_bl]

        for j in range(nebins):
            e_bl = (sig_data["ENERGY"] >= ebins0[j]) & (
                sig_data["ENERGY"] < (ebins1[j])
            )

            cnts_mat[i, j] = ev2quad_cnts(sig_data[e_bl])

    return cnts_mat


def get_cnts(ev, t_bins0, t_bins1, ebin_inds, nebins):
    ntbins = len(t_bins0)
    cnts = np.zeros((ntbins, nebins))

    for i in range(ntbins):
        blt = (ev["TIME"] >= t_bins0[i]) & (ev["TIME"] < t_bins1[i])
        ebin_inds_ = ebin_inds[blt]

        for j in range(nebins):
            cnts[i, j] = np.sum(ebin_inds_ == j)

    return cnts


def get_data_cube(tbins0, tbins1, ebins0, ebins1, evd, dmask_bl):
    ntbins = len(tbins0)
    nebins = len(ebins0)
    ndets = np.sum(dmask_bl)

    data_cube = np.zeros((ntbins, nebins, ndets), dtype=np.int)

    for i in range(ntbins):
        blt = (evd["TIME"] >= tbins0[i]) & (evd["TIME"] < tbins1[i])
        ev_ = evd[blt]

        data_cube[i] = np.array(det2dpis(ev_, ebins0, ebins1, bl_dmask=dmask_bl))

    return data_cube


def main(args):
    ebins0 = np.array([14.0, 20.0, 26.0, 36.3, 51.1, 70.9, 91.7, 118.2, 151.4])
    ebins1 = np.append(ebins0[1:], [194.9])
    ebins0 = np.array([14.0, 24.0, 36.3, 55.4, 80.0, 120.7])
    ebins1 = np.append(ebins0[1:], [194.9])

    nebins = len(ebins0)

    ev_data = fits.open(args.evf)[1].data
    dmask = fits.open(args.dmask)[0].data

    bl_dmask = dmask == 0

    good_dt0 = args.bkgt0 - args.trigtime - 1.0
    good_dt1 = 20.0
    trig_time = args.trigtime
    good_t0 = trig_time + good_dt0
    good_t1 = trig_time + good_dt1

    mask_vals = mask_detxy(dmask, ev_data)

    bl_ev = (
        (ev_data["TIME"] > good_t0)
        & (ev_data["TIME"] < good_t1)
        & (ev_data["EVENT_FLAGS"] < 1)
        & (mask_vals == 0)
        & (ev_data["ENERGY"] <= 194.9)
        & (ev_data["ENERGY"] >= 14.0)
    )
    ev_data0 = ev_data[bl_ev]

    ebins = np.append(ebins0, [ebins1[-1]])
    ebin_ind = np.digitize(ev_data0["ENERGY"], ebins) - 1

    bkg_bl = (ev_data0["TIME"] > args.bkgt0) & (
        ev_data0["TIME"] < (args.bkgt0 + args.bkgdt)
    )
    bkg_data = ev_data0[bkg_bl]

    bkg_data_dpis = det2dpis(bkg_data, ebins0, ebins1)

    bkg_quad_cnts = np.array([np.array(dpi2cnts_perquad(dpi)) for dpi in bkg_data_dpis])

    ndets_quad = dmask2ndets_perquad(dmask)

    # bkg_cnts = np.array([np.sum(dpi[(dmask==0)]) for dpi in bkg_data_dpis])
    print(bkg_quad_cnts)
    print(bkg_quad_cnts / args.bkgdt)

    bkg_err = 2.0 * np.sqrt(bkg_quad_cnts)

    tstep = 0.064
    bin_size = 0.128
    t_bins0 = np.arange(-15.008, 15.008, tstep) + trig_time
    t_bins1 = t_bins0 + bin_size
    ntbins = len(t_bins0)
    print(ntbins)

    # cnts_per_tbin = get_cnts_per_tbins(t_bins0, t_bins1, ebins0, ebins1,\
    #                                  ev_data0, dmask)

    # _data_cube = get_data_cube(t_bins0, t_bins1, ebins0, ebins1,\
    #                             ev_data0, bl_dmask)

    quad_cnts_mat = get_quad_cnts_tbins(t_bins0, t_bins1, ebins0, ebins1, ev_data0)

    drm_fnames = sorted([fn for fn in os.listdir(args.drmdir) if "drm_" in fn])

    imxs = np.array([float(fn.split("_")[1]) for fn in drm_fnames])
    imys = np.array([float(fn.split("_")[2]) for fn in drm_fnames])

    drm = fits.open(os.path.join(args.drmdir, drm_fnames[0]))
    ebin_ind_edges = get_ebin_ind_edges(drm, ebins0, ebins1)

    ind_ax = np.linspace(-1.5, 3.5, 20 * 5 + 1)

    # cnts_per_tbin = get_cnts(ev_data0, t_bins0, t_bins1, ebin_ind, nebins)

    abs_cor = get_abs_cor_rates(0.0, 0.0, drm)

    cnts_intp = get_cnts_intp_obj(ind_ax, drm, ebin_ind_edges, abs_cor)

    names = ["tstart", "tstop", "bkg_llh", "sig_llh", "Nsig", "plaw_ind"]

    N_dbl_dt = args.ndbl

    cnts_norm = cnts_intp(1.0)

    tabs = []

    quad_dicts = {
        "all": {"quads": [0, 1, 2, 3], "drm_fname": "drm_0.200_0.150_.fits"},
        "left": {"quads": [0, 1], "drm_fname": "drm_1.000_0.150_.fits"},
        "top": {"quads": [1, 2], "drm_fname": "drm_0.000_-0.500_.fits"},
        "right": {"quads": [2, 3], "drm_fname": "drm_-1.000_0.150_.fits"},
        "bottom": {"quads": [3, 0], "drm_fname": "drm_0.000_0.450_.fits"},
        "quad0": {"quads": [0], "drm_fname": "drm_1.000_0.500_.fits"},
        "quad1": {"quads": [1], "drm_fname": "drm_0.800_-0.400_.fits"},
        "quad2": {"quads": [2], "drm_fname": "drm_-0.750_-0.450_.fits"},
        "quad3": {"quads": [3], "drm_fname": "drm_-1.100_0.500_.fits"},
    }

    for ii in range(N_dbl_dt):
        for direction, quad_dict in quad_dicts.items():
            tab = Table()

            drm = fits.open(os.path.join(args.drmdir, quad_dict["drm_fname"]))

            imx = float(quad_dict["drm_fname"].split("_")[1])
            imy = float(quad_dict["drm_fname"].split("_")[2])

            abs_cor = get_abs_cor_rates(imx, imy, drm)

            # cnts_per_tbin = np.sum(data_cube[:,:,np.where(fp_msked)[0]], axis=2)

            cnts_per_tbin = np.sum(
                [quad_cnts_mat[:, :, q] for q in quad_dict["quads"]], axis=0
            )

            bkg_llh_tbins = np.zeros(ntbins)
            # bf_bkg_rates = np.zeros((ntbins, nebins))

            bkg_cnts = np.sum([bkg_quad_cnts[:, q] for q in quad_dict["quads"]], axis=0)

            bkg_err = 5.0 * np.sqrt(bkg_cnts)
            print(imx, imy)
            print(bkg_err)
            print(bin_size * bkg_err / args.bkgdt)

            for i in range(ntbins):
                bkg_llh_tbins[i] = rates_llh(
                    cnts_per_tbin[i],
                    0.0,
                    cnts_norm,
                    bkg_cnts,
                    bkg_err,
                    bin_size,
                    args.bkgdt,
                )

            bf_nsigs = np.zeros(ntbins)
            bf_inds = np.zeros(ntbins)
            llhs = np.zeros(ntbins)

            for i in range(ntbins):
                x0 = [1.0, 1.0]
                _args = (
                    cnts_per_tbin[i],
                    bkg_cnts,
                    bkg_err,
                    bin_size,
                    args.bkgdt,
                    cnts_intp,
                )

                # res = rate_llh2min(x0, *args)

                res = optimize.fmin(
                    rate_llh2min, x0, args=_args, disp=False, full_output=True
                )

                bf_nsigs[i] = 10.0 ** res[0][0]
                bf_inds[i] = res[0][1]
                llhs[i] = res[1]

            tab["tstart"] = t_bins0
            tab["tstop"] = t_bins1
            tab["bkg_llh"] = bkg_llh_tbins
            tab["sig_llh"] = llhs
            tab["Nsig"] = bf_nsigs
            tab["plaw_ind"] = bf_inds
            tab["imx"] = imx * np.ones_like(bf_inds)
            tab["imy"] = imy * np.ones_like(bf_inds)
            tab["counts"] = np.sum(cnts_per_tbin, axis=1)

            tabs.append(tab)

        # cnts_per_tbin, t_bins0, t_bins1 =\
        #    double_up_tbins(cnts_per_tbin, t_bins0, t_bins1)

        # tstep = t_bins1[0] - t_bins0[0]
        # ntbins -= 1

        tstep *= 2
        bin_size *= 2
        t_bins0 = np.arange(-15.008, 15.008, tstep) + trig_time
        t_bins1 = t_bins0 + bin_size
        ntbins = len(t_bins0)
        print(ntbins)

        quad_cnts_mat = get_quad_cnts_tbins(t_bins0, t_bins1, ebins0, ebins1, ev_data0)

        # cnts_per_tbin = get_cnts(ev_data0, t_bins0, t_bins1, ebin_ind, nebins)

    tab = vstack(tabs)

    fname = os.path.join(args.obsid, "rate_llhs_trigtime_%.1f_.fits" % (args.trigtime))

    dt_tot = np.max(tab["tstop"]) - np.min(tab["tstart"])
    llhrs = tab["bkg_llh"] - tab["sig_llh"]
    exps = tab["tstop"] - tab["tstart"]
    pvals = stats.chi2.sf(2.0 * llhrs, 1)
    Nexps = args.ndbl + 1
    for i in range(Nexps):
        bl_exp = np.isclose(exps, 0.128 * (2 ** (i)))
        pvals[bl_exp] *= np.sum(bl_exp) / dt_tot
    pvals = 1.0 - np.exp(-pvals)

    tab["pval"] = pvals

    tab.write(fname)

    return


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--obsid", type=str, help="Obsid as a string, as it appears in file names"
    )
    parser.add_argument("--evf", type=str, help="Event File Name")
    parser.add_argument("--e0", type=float, help="Min energy", default=14.0)
    parser.add_argument("--e1", type=float, help="Max energy", default=194.9)
    parser.add_argument("--dmask", type=str, help="Detmask fname")
    parser.add_argument(
        "--drmdir",
        type=str,
        help="Directory to find the DRMs",
        default="/gpfs/scratch/jjd330/bat_data/drms4quads/",
    )
    parser.add_argument("--trigtime", type=float, help="Trigger time in MET seconds")
    parser.add_argument("--bkgt0", type=float, help="Bkg start time in MET seconds")
    parser.add_argument("--bkgdt", type=float, help="Bkg duration time in seconds")
    parser.add_argument(
        "--ndbl",
        type=int,
        help="Number of times to double the time bin duration",
        default=5,
    )
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = cli()
    main(args)
