#!/usr/bin/python
## TODO:
## - the plot is plotting the null model average, not the best fit one.
## is MLE distribution of allele frequencies shifted over off the peak for small bin sizes?
## - finish wiki pages.
## - test distribution/usability.

import argparse, collections, sys
import matplotlib, numpy, pylab, scipy.stats

def load_table(fin, binsize, verbose, filt):
    temp = collections.defaultdict(lambda : numpy.zeros(2))
    for line in fin:
        if line.startswith("#"): continue
        line = line.strip().split()
        line = map(float, line[:3])
        # if len(line) <= 1: continue
        a, b = line[1:3]
        if filt and (a <= 0 or b <= 0): # we might miss really informative SNPs, but we're probably just missing fixated markers... so skip them
            if verbose and a+b>0: print >>sys.stderr, "Skipping", line
            continue
        temp[line[0] / binsize] += (a,b)
    fin.close()


    if filt:
        # Filter highly-outlying counts.  Preprocessing should take
        # care of this, but this is another layer of help.
        median = numpy.median(temp.values())

        # Filter by median absolute deviation.
        cutoff = 20 * numpy.median(abs(numpy.array(temp.values()) - median)) + median
        print >>sys.stderr, "cutoff:", cutoff

        for k,v in temp.iteritems():
            if sum(v) > cutoff:
                print >>sys.stderr, "Filtering allele counts:", v
                temp[k] = v-v

    means = numpy.zeros(max(temp.keys())+1)
    counts = numpy.zeros(max(temp.keys())+1)
    variances = numpy.zeros(max(temp.keys())+1) + float("inf")
    for loc, (a,b) in temp.iteritems():
        p = 1.0*a/(a+b)
        means[loc] = a
        counts[loc] = a+b
        if a+b > 0:
            variances[loc] = p*(1.0-p) * (a+b)

    return means, variances, counts

# Return the log of the pdf of the normal distribution parametrized by
# mu and sigma.
def lognormpdf(x, mu, sigma):
    return -0.5*numpy.log(2*numpy.pi) - numpy.log(sigma) + (-(x-mu)**2.0/2.0/sigma**2.0)

# this calculates posterior estimates of the means and variances
# at each point in the sequence
# 
# so: P(x_i | y) = N(x_i | mu_pstr, V_pstr)
#
def kalman(y, y_var, d, T, N, p):
    mu = numpy.zeros(T)
    V = numpy.zeros(T)
    P = numpy.zeros(T)

    V_pstr = numpy.zeros(T)
    mu_pstr = numpy.zeros(T)

    c = numpy.ones(T)

    mu_initial = 0.5*N # initial parameters, assumed given
    V_initial = 0.25*N # ditto

    A = (1.0 - 2.0*p)
    C = 1.0 * d / N
    S = p*(1.0-p)*N

    K = V_initial*C[0]/(C[0]**2.0*V_initial + y_var[0])
    mu[0] = mu_initial + K*(y[0] - C[0]*mu_initial)
    V[0] = (1.0-K*C[0])*V_initial
    # P[0] = A**2.0*V_initial + S
    if y_var[0] != float("inf"):
        c[0] = scipy.stats.norm.pdf(y[0], C[0]*mu_initial, numpy.sqrt(C[0]**2.0*V_initial + y_var[0]))
    else:
        c[0] = 1.0

    # Forward pass
    for i in xrange(1,T):
        # P[i] = A**2.0 * V[i] + S # how did this work?
        if i == 1:
            P[i-1] = A**2.0*V_initial + S
        else:
            P[i-1] = A**2.0*V[i-1] + S
        if y_var[i] == float("inf"): # no observation here
            K = 0
            c[i] = 1.0
        else:
            K = P[i-1]*C[i]/(C[i]**2.0*P[i-1]+y_var[i])
            c[i] = scipy.stats.norm.pdf(y[i], C[i]*(A*mu[i-1]+p*N), numpy.sqrt(C[i]**2.0*P[i-1] + y_var[i]))
            c[i] = max(c[i], 1e-300)
        mu[i] = A * mu[i-1] + N*p + K * (y[i] - C[i]*(A*mu[i-1] + N*p))
        V[i] = (1.0-K*C[i])*P[i-1]

    V_pstr[-1] = V[-1]
    mu_pstr[-1] = mu[-1]

    logLik = numpy.sum(numpy.log(c))

    # backward pass
    for i in xrange(T-2,-1,-1):
        J = V[i]*A/P[i]
        mu_pstr[i] = mu[i] + J * (mu_pstr[i+1] - A*(mu[i]) - N*p)
        V_pstr[i] = V[i] + J**2.0 * (V_pstr[i+1] - P[i])

    return mu_pstr, V_pstr, logLik

def calcLODs_multicoupled(mu_pstr_vec, V_pstr_vec, T, N):
    LOD = numpy.zeros(T)
    mu_MLE = numpy.zeros(T)

    # Initial parameters (null model for genomic region)
    mu_initial = 0.5*N
    V_initial = 0.25*N
    
    # We're trying to calculate LR(i) = max_p' Pr(y | p=p') / Pr (y | p=1/2)
    #     = max_p' int_0^1 Pr(x_i=j | y) / Pr(x_i=j) * Pr(x_i=j | p=p') dj
    # 
    # We compute it by discretizing the choices for p' and approximating
    # the values the integral takes on for each choice.

    # Grid for p':
    delta = 0.0025
    x = numpy.arange(delta, 1.0-delta+delta/2, delta)

    # Precompute values of Pr(x_i=j | p=p') (for each value of p'):
    p_precomp = numpy.array([scipy.stats.norm.pdf(N*x, N*p_alt, numpy.sqrt(p_alt*(1.0-p_alt)*N)) for p_alt in x])

    # This works because these quantities do not depend on the
    # observed data (y, through mu_pstr or V_pstr) and are shared
    # across all timepoints (indexed by i in the loop below).

    # log Pr(x_i=j) (unconditional model, from the stationary distribution)
    logreweighter = lognormpdf(N*x, mu_initial, numpy.sqrt(V_initial))

    for i in xrange(T):
        logallsums = numpy.zeros(len(x))
        for mu_pstr, V_pstr in zip(mu_pstr_vec, V_pstr_vec):
            # log( Pr(x_i=j | y)) - log( Pr(x_i=j))
            logtemp = lognormpdf(N*x, mu_pstr[i], numpy.sqrt(V_pstr[i])) - logreweighter
            scaler = logtemp.max() # We use this trick to keep the numbers in range: X = C * X / C, etc.
            logallsums += scaler + numpy.log(1e-300 + numpy.dot(p_precomp, numpy.exp(logtemp - scaler)))

        # Now, we calculate a bunch of integrals with grids by
        # multiplying by the rows of p_precomp.  Each row
        # corresponds to a value of p' that we want to optimize
        # over.  We pick the best p'.
        p_alt = x[logallsums.argmax()] * N
        mu_MLE[i] = p_alt

        # LOD[i] = numpy.log10(N*(x[1]-x[0]) * allsums.max())
        LOD[i] = numpy.log10(N) + numpy.log10(x[1]-x[0]) + logallsums.max() / numpy.log(10.0)
        
        # assert(LOD[i] > -1e-6)
        # assert(LOD[i] == LOD[i]) # check for nan
        # assert(LOD[i] != LOD[i]+1) # check for +/- inf

    return LOD, mu_MLE

def doOutput():
    pass

def parseArgs():
    parser = argparse.ArgumentParser(description="Multipool description")

    parser.add_argument("-f", "--countfile[s]", type=argparse.FileType("r"), nargs=argparse.REMAINDER, help="Input file[s], space separated for multiple files.", required=True, dest="fins")
    parser.add_argument("-n", "--individuals", type=int, help="Individuals in each pool", required=True, dest="N")
    parser.add_argument("-m", "--mode", choices=["replicates", "contrast", "combine"], default="replicates", help="Mode for statistical testing.  Default: replicates.", dest="mode")
    parser.add_argument("-r", "--resolution", type=float, default=100, help="Bin size for discrete model.  Default: 100 bp.", dest="res")
    parser.add_argument("-c", "--centimorgan", type=float, default=3300, help="Length of a centimorgan, in base pairs.  Default: 3300 (yeast average).", dest="cM")
    parser.add_argument("-t", "--truncate", type=bool, default=True, help="Truncate possibly fixated (erroneous) markers.  Default: true.", dest="filter")

    parser.add_argument("-v", "--version", action="version", version="%(prog)s 0.8")

    return parser.parse_args()

if __name__ == "__main__":
    args = parseArgs()

    print >>sys.stderr, "Multipool version:", "TODO"
    print >>sys.stderr, "Python version:", sys.version
    print >>sys.stderr, "Scipy version:", scipy.__version__
    print >>sys.stderr, "Numpy version:", numpy.__version__
    print >>sys.stderr, "Matplotlib version:", matplotlib.__version__
    
    N = args.N
    res = args.res
    p = res/100.0/args.cM # was 3000.0
    fins = args.fins
    filt = args.filter

    REPLICATES = (args.mode == "replicates")
    COMBINE_DATA = (args.mode == "combine")

    SIMPLE = False
    MOREINFO = True # what's this?
    
    print >>sys.stderr, "Recombination fraction:", p, "in cM:", 1.0*res/p/100.0

    y,y_var,d = load_table(fins[0], res, False, filt)

    if len(fins) > 1:
        y2, y_var2, d2 = [], [], []
        for fin in fins[1:]:
            temp1, temp2, temp3 = load_table(fin, res, False, filt)
            y2.append(temp1)
            y_var2.append(temp2)
            d2.append(temp3)
    else:
        y2 = None

    print >>sys.stderr, "Loaded %d informative reads" % sum(d)

    if y2 is None:
        T = len(y) # observations (max time)
    else:
        T = min([len(temp) for temp in y2])
        y = y[:T]
        y_var = y_var[:T]
        d = d[:T]

        for i in xrange(len(y2)):
            y2[i] = y2[i][:T]
            y_var2[i] = y_var2[i][:T]
            d2[i] = d2[i][:T]

        if COMBINE_DATA: # this doesn't work any more (y2, etc. are now lists)
            d += d2
            y = (y+y2)
            y_var = (y_var + y_var2)
            y2 = None

    start, stop = 0,0 # T/2,T # 2*T/5, T # 2*T/5, T # 2*T/11, T
    y_var[start:stop] = float("inf")
    if y2 is not None:
        for i in xrange(len(y_var2)):
            y_var2[i][start:stop] = float("inf")
    d[start:stop] = 0
    if y2 is not None: 
        for i in xrange(len(d2)):
            d2[i][start:stop] = 0

    X = numpy.arange(0, T*res, res)

    mu_pstr, V_pstr, logLik = kalman(y, y_var, d, T, N, p)
    if y2 is not None:
        mu_pstr2 = []
        V_pstr2 = []
        old = numpy.seterr(all="ignore")
        for curr_y2, curr_y_var2, curr_d2 in zip(y2, y_var2, d2):
            curr_mu_pstr2, curr_V_pstr2, ignored = kalman(curr_y2, curr_y_var2, curr_d2, T, N, p)
            pylab.plot(X, curr_y2/curr_d2 , "+", alpha=0.6)
            pylab.plot(X, curr_mu_pstr2/N, 'r', lw=2)
            mu_pstr2.append(curr_mu_pstr2)
            V_pstr2.append(curr_V_pstr2)
        numpy.seterr(**old)

    LOD, mu_MLE = calcLODs_multicoupled([mu_pstr], [V_pstr], T, N)

    if y2 is not None:
        LOD3, mu_MLE3 = calcLODs_multicoupled(mu_pstr2, V_pstr2, T, N)

        if REPLICATES:
            LOD = LOD3
            mu_MLE = mu_MLE3
        else:
            LOD2, mu_MLE2 = calcLODs_multicoupled([mu_pstr2[1]], [V_pstr2[1]], T, N)
            assert(len(mu_pstr2) == 2)
            LOD = LOD + LOD2 - LOD3

    if False and not SIMPLE:
        pylab.subplot(211)

    numpy.seterr(divide="ignore")
    pylab.plot(X, y/d, "r+", alpha=0.6)
    numpy.seterr(divide="warn")

    pylab.xlabel("bp (%d bp loci)" % res)
    pylab.ylabel("Allele frequency")

    # pylab.plot(X, mu_pstr/N, 'r', lw=2)
    # pylab.plot(X, mu_MLE/N, 'r', alpha=0.5, lw=2)

    if y2 is not None:
        for val, alpha in [(0.025, 0.3), (0.005, 0.1), (0.0005, 0.05)]:
            for curr_mu_pstr2, curr_V_pstr2 in zip(mu_pstr2, V_pstr2):
                CI = scipy.stats.norm.isf(val, 0, numpy.sqrt(curr_V_pstr2))
                # pylab.fill_between(X, (curr_mu_pstr2 - CI)/N, (curr_mu_pstr2 + CI)/N, alpha=alpha)
                pylab.fill_between(X, (curr_mu_pstr2 - CI)/N, (curr_mu_pstr2 + CI)/N, color='r', alpha=alpha)
    else:
        for val, alpha in [(0.025, 0.3), (0.005, 0.1), (0.0005, 0.05)]:
            CI = scipy.stats.norm.isf(val, 0, numpy.sqrt(V_pstr))
            pylab.fill_between(X, (mu_MLE - CI)/N, (mu_MLE + CI)/N, color='r', alpha=alpha)
            # pylab.fill_between(X, (mu_pstr - CI)/N, (mu_pstr + CI)/N, color='r', alpha=alpha)


    # CI = scipy.stats.norm.isf(0.025, 0, numpy.sqrt(V))
    # pylab.fill_between(numpy.arange(T), mu - CI, mu + CI, color='y', alpha=0.3)
    # pylab.plot(V, '--')
    # pylab.plot(V_pstr, '--', lw=2)
    pylab.axhline(0.5, color='k', ls=':')

    temp = numpy.exp(LOD) / numpy.sum(numpy.exp(LOD))

    left = temp.argmax()
    right = left
    cumul = temp[left]
    while cumul < 0.50: # and (left >= 0 and right < T):
        if temp[left] >= temp[right] and left > 0 or right == T-1 and left > 0:
            left -= 1
            cumul += temp[left]
        elif right < T-1:
            right += 1
            cumul += temp[right]
        else:
            break

    print >>sys.stderr, "Other 50% credible interval spans", left*res, right*res, "length is:", (right-left)*res
              
    targStart = left*res
    targStop = right*res
    pylab.fill_between([targStart-res/2,targStop-res/2], 0, 1, color="k", alpha=0.2)

    cumul, mean = 0.0, 0.0
    left, right = None, None
    for i,val in enumerate(temp):
        cumul += val
        if cumul >= 0.05 and left is None:
            left = i-1
        if cumul >= 0.95 and right is None:
            right = i
        mean += val*i*res
    if left is None: left = 0
    if right is None: right = T

    print >>sys.stderr, "90% credible interval spans", left*res, right*res, "length is:", (right-left)*res, "mean:", mean, "mode:", temp.argmax()*res

    pylab.axis([0,T*res,0,1])
    # pylab.axis([0,400000,0,1])
    if not SIMPLE:
        pylab.twinx()
        pylab.ylabel("LOD score")
        pylab.plot(X, LOD, 'g-', lw=2)

    if not SIMPLE:
        if False and MOREINFO: pylab.plot(X, temp / numpy.max(temp) * numpy.max(LOD), 'g')
        # pylab.plot(X, scipy.stats.norm.pdf(X, mean, stdev) / scipy.stats.norm.pdf(mean, mean, stdev) * numpy.max(LOD), 'g:')
    # print LOD


    if False and not SIMPLE:
        pylab.subplot(212)
    if N < 10000:
        posteriors = numpy.zeros((N,T))
        for c in xrange(T):
            posteriors[:,c] = scipy.stats.norm.pdf(numpy.arange(0,1.0,1.0/N), mu_pstr[c]/N, numpy.sqrt(V_pstr[c])/N)
            posteriors[:,c] /= numpy.sum(posteriors[:,c])
        if False and not SIMPLE:
            pylab.imshow((numpy.log10(posteriors)), interpolation="nearest", origin="lower", cmap=pylab.get_cmap("YlGnBu"))

    if True:
        maxLOD = LOD.max()
        maxIndex = LOD.argmax()
        print >>sys.stderr, "max multi-locus LOD score at:", maxLOD, maxIndex*res
        index = maxIndex
        while index >= 0 and LOD[index] > maxLOD-1.0:
            index -= 1
        left = index
        print >>sys.stderr, "1-LOD interval from ", index*res,
        index = maxIndex
        while index < T and LOD[index] > maxLOD-1.0:
            index += 1
        print >>sys.stderr, "to", index*res, "length is:", (index-left)*res

        # print "p_alternate at highest LOD score segment:", (means[maxIndex] + 1.0) / (N+2.0)
        D = 30
        try:
            print >>sys.stderr, "sublocalized best location:", numpy.sum(numpy.arange(res*(maxIndex-D),res*(maxIndex+D+1),res)*numpy.exp(LOD[maxIndex-D:maxIndex+D+1])) / numpy.sum(numpy.exp(LOD[maxIndex-D:maxIndex+D+1]))
        except ValueError:
            pass

    pylab.axis([0,T*res,LOD.min(),LOD.max()+3])
    # pylab.axis([0,400000,0,LOD.max()+3])
    pylab.show()