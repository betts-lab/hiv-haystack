import pysam
from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from collections import defaultdict
import argparse
import os
import csv
import re
import subprocess
from pprint import pprint
from termcolor import cprint

printRed = lambda x: cprint(x, "red")
printGreen = lambda x: cprint(x, "green")
printCyan = lambda x: cprint(x, "cyan")
printBlue = lambda x: cprint(x, "blue")
printCyanOnGrey = lambda x: cprint(x, "cyan", "on_grey")

def getProviralFastaIDs(fafile, recordSeqs):
  ids = []
  for record in SeqIO.parse(fafile, format = "fasta"):
    ids.append(record.id)
    recordSeqs[record.id].append(record.seq)

  return ids


def extractCellBarcode(read):
  # accept only CB tag because it passes the allowlist set by 10X
  tags = dict(read.tags)
  if "CB" in tags:
      barcode = tags["CB"]
  else:
      barcode = None

  return barcode


def getLTRseq(seq, start, end):
  ltrSeq = seq[start - 1:end]
  return ltrSeq


def parseLTRMatches(LTRargs, proviralSeqs, position = False, endBuffer = 20):
  LTRdict = defaultdict(lambda: {
    "5p": None,
    "5pRevComp": None,
    "5pStart": None,
    "5pEnd": None,
    "3p": None,
    "3pStart": None,
    "3pEnd": None,
    "3pRevComp": None})

  if position:
    marks = [int(x) for x in LTRargs.split(",")]

    for k in proviralSeqs:
      proviralSeq = proviralSeqs[k][0]
      ltr5p = getLTRseq(proviralSeq, marks[0], marks[1])
      ltr3p = getLTRseq(proviralSeq, marks[2], marks[3])

      LTRdict[k]["5p"] = ltr5p
      LTRdict[k]["5pStart"] = marks[0]
      LTRdict[k]["5pEnd"] = marks[1]
      LTRdict[k]["5pRevComp"] = ltr5p.reverse_complement()
      LTRdict[k]["3p"] = ltr3p
      LTRdict[k]["5pStart"] = marks[0]
      LTRdict[k]["5pEnd"] = marks[1]
      LTRdict[k]["3pRevComp"] = ltr3p.reverse_complement()

  else:
    with open(LTRargs, "r") as fhandle:
      rd = csv.reader(fhandle, delimiter = "\t")

      for row in rd:
        # index 1 = subject ID (i.e. the original sample's viral fasta ID)
        # index 2 = percent match
        # index 6 = query start
        # index 7 = query end
        # index 8 = subject start
        # index 9 = subject end
        
        subjID = row[1]
        # qstart = row[6]
        # qend = row[7]
        sstart = int(row[8])
        send = int(row[9])
        slen = len(proviralSeqs[subjID][0])
        
        # must be at least 550bp long
        if abs(send - sstart) < 550:
          continue
        elif sstart < endBuffer:
          seq = getLTRseq(proviralSeqs[subjID][0], 1, send)
          LTRdict[subjID]["5p"] = seq
          LTRdict[subjID]["5pStart"] = 1
          LTRdict[subjID]["5pEnd"] = send
          LTRdict[subjID]["5pRevComp"] = seq.reverse_complement()
        elif slen - send < endBuffer:
          seq = getLTRseq(proviralSeqs[subjID][0], sstart, slen)
          LTRdict[subjID]["3p"] = seq
          LTRdict[subjID]["3pStart"] = sstart
          LTRdict[subjID]["3pEnd"] = slen          
          LTRdict[subjID]["3pRevComp"] = seq.reverse_complement()

  return LTRdict


def getSoftClip(read, clipMinLen, softClipPad, useAlt = None):
  # cutoff same as epiVIA
  cigar = read.cigartuples
  clippedFrag = Seq("")
  adjacentFrag = Seq("")

  clip5Present = False
  clip3Present = False

  if useAlt is None:
    # loop through cigar to make sure there's only 1 soft clip
    if read.cigarstring.count("S") > 1:
      return None

    passing5p = cigar[0][0] == 4 and cigar[0][1] >= clipMinLen
    passing3p = cigar[-1][0] == 4 and cigar[-1][1] >= clipMinLen

    clipLen5p = cigar[0][1]
    clipLen3p = cigar[-1][1]

  else:
    if useAlt["cigarstring"].count("S") > 1:
      return None
    
    readAltCigar = separateCigarString(useAlt["cigarstring"])
    passing5p = readAltCigar[0][1] == "S" and int(readAltCigar[0][0]) >= clipMinLen
    passing3p = readAltCigar[-1][1] == "S" and int(readAltCigar[-1][0]) >= clipMinLen

    clipLen5p = int(readAltCigar[0][0])
    clipLen3p = int(readAltCigar[-1][0])

  if passing5p:
    clippedFrag = read.seq[0:clipLen5p]
    adjacentFrag = read.seq[clipLen5p:clipLen5p + softClipPad]
    clip5Present = True
  
  if passing3p:
    clippedFrag = read.seq[clipLen3p * -1: ]
    adjacentFrag = read.seq[clipLen3p * -1 - softClipPad: clipLen3p * -1]
    clip3Present = True    

  # clip can only be present at one end
  if clip5Present and clip3Present:
    return None
  elif not clip5Present and not clip3Present:
    return None
  else:
    clippedFragObj = {
      "clippedFrag": clippedFrag,
      "adjacentFrag": adjacentFrag,
      "useAlt": useAlt,
      "clip5Present": clip5Present,
      "clip3Present": clip3Present}

    return clippedFragObj


def isSoftClipProviral(read, proviralLTRSeqs, clipMinLen = 11, softClipPad = 3, ignoreOrient = False):
  clippedFragObj = getSoftClip(read, clipMinLen, softClipPad)
  
  # skip if no clipped fragment long enough is found
  if clippedFragObj is None:
    return False

  # soft clip position has to match correct forward/reverse strandness of read
  # if 5', read has to be forward to not exit
  if not ignoreOrient:
    if clippedFragObj["clip5Present"] and read.flag & 16:
      return False
    # if 3', read has to be reverse strand to not exit
    elif clippedFragObj["clip3Present"] and read.flag & 32:
      return False

  strClippedFrag = str(clippedFragObj["clippedFrag"])

  # skip if there are any characters other than ATGC 
  if bool(re.compile(r'[^ATGC]').search(strClippedFrag)):
    return False
  
  hits = {
    "plus": [],
    "plusIds": [],
    "minus" : [],
    "minusIds": [],
    "clip5P": clippedFragObj["clip5Present"],
    "clip3P": clippedFragObj["clip3Present"]}

  allowedLTRKeys = []
  # only allow specific keys based on orientation
  if clippedFragObj["clip5Present"]:
    allowedLTRKeys = ["3p", "5pRevComp"]
  elif clippedFragObj["clip3Present"]:
    allowedLTRKeys = ["5p", "3pRevComp"]

  # find hits...
  foundHit = False
  for key in proviralLTRSeqs:
    keyPair = proviralLTRSeqs[key]

    for ltrType in allowedLTRKeys:
      s = keyPair[ltrType]
      if s is None:
        continue

      # find orientation
      orient = "plus" if ltrType == "5p" or ltrType == "3p" else "minus"

      matches = [x.start() for x in re.finditer(strClippedFrag, str(s))]
      if len(matches) == 0:
        continue

      ltrLen = len(str(s))
      # check if match is within soft buffer zone
      # needs to pass min(matches) <= softClipPad or max(matches) + len(strClippedFrag) >= ltrLen - softClipPad:
      if (ltrType == "5p" or ltrType == "3pRevComp") and min(matches) > softClipPad:
        continue
      elif (ltrType == "3p" or ltrType == "5pRevComp") and max(matches) + len(strClippedFrag) < ltrLen - softClipPad:
        continue

      # check if the adjacent host clips could have also been aligned to the viral LTR,
      # thus explaining the lack of viral clip not being at either end of LTR
      ltrEnd = ""
      if (ltrType == "5p" or ltrType == "3pRevComp") and min(matches) != 0:
        ltrEnd = str(s)[0:min(matches)]
        hostAdjacentBp = str(read.seq)[-len(strClippedFrag) - len(ltrEnd): -len(strClippedFrag)]

      elif (ltrType == "3p" or ltrType == "5pRevComp") and max(matches) != ltrLen - softClipPad:
        adjacentBpNum = ltrLen - max(matches) - len(strClippedFrag)
        ltrEnd = str(s)[max(matches) + len(strClippedFrag): ltrLen]
        hostAdjacentBp = str(read.seq)[len(strClippedFrag): len(strClippedFrag) + adjacentBpNum]

      if ltrEnd != "" and ltrEnd != hostAdjacentBp:
        print("{}: Viral clip not found at the end of LTR".format(read.query_name))
        continue

      # passes all checks!
      print("{}: chimeric match found".format(read.query_name))

      hits[orient].append(matches)
      hits[orient + "Ids"].append(key + "___" + ltrType)
      foundHit = True

  # can only be plus orientation OR minus orientation only
  if foundHit and len(hits["plus"]) != 0 and len(hits["minus"]) == 0:
    return hits
  elif foundHit and len(hits["minus"]) != 0 and len(hits["plus"]) == 0:
    return hits
  else:
    return False


def parseHostReadsWithPotentialChimera(readPairs, proviralLTRSeqs, clipMinLen):
  validHits = []
  validReads = []

  for key in readPairs:
    # only allow one read mate to have soft clip
    if len(readPairs[key]) != 1:
      continue 
    
    read = readPairs[key][0]

    # must contain valid cell barcode passing allowlist
    if extractCellBarcode(read) is None:
     continue
    
    potentialHits = isSoftClipProviral(read, proviralLTRSeqs, clipMinLen)
    
    if potentialHits:
      validHits.append(potentialHits)
      validReads.append(read)

  returnVal = {"validHits": validHits, "validReads": validReads}
  return returnVal


def getAltAlign(read):
  if not read.has_tag("XA"):
    return None

  altAlignRaw = read.get_tag("XA")
  
  # remove last semicolon
  altAlignRaw = altAlignRaw[:-1]

  altAligns = altAlignRaw.split(";")
  altAligns = [x.split(",") for x in altAligns]

  return altAligns


def separateCigarString(cigarstring):
  cigarSep = re.findall(r"(\d+\w)", cigarstring)
  cigarSepExpanded = [re.split(r"(\d+)", x)[1:3] for x in cigarSep]

  return cigarSep


def checkForPotentialHostClip(read, refLen, proviralSeqs, clipMinLen = 17, useAlts = None, softClipPad = 3):
  readInfo = {
    "start": read.reference_start,
    "cigar": read.cigar,
    "cigarstring": read.cigarstring
  }

  if useAlts is not None:
    readInfo["start"] = int(useAlts[1].lstrip("[+-]"))
    readInfo["cigarstring"] = useAlts[2]

    readClip = getSoftClip(read, clipMinLen, softClipPad, useAlt = readInfo)
  
  else:
    readClip = getSoftClip(read, clipMinLen, softClipPad)

  readNear5p = readInfo["start"] <= softClipPad
  readNear3p = readInfo["start"] >= refLen - read.query_length - softClipPad - 1

  if readClip is None or (not readNear5p and not readNear3p):
    # print("{} is not close enough to LTR".format(read1.query_name))
    return None

  returnObj = {
    "read": read,
    "hostSoftClip": readClip
  }

  clip = readClip["clippedFrag"]
  if readNear5p:
    provirusStart = readInfo["start"]

    clipPartial = clip[-1 * (provirusStart - 1): ]
    provirusActual = proviralSeqs[read.reference_name][0][1:provirusStart]
    print("HERE {} {}".format(read.to_string, provirusStart))

    if provirusStart == 1:
      return returnObj
    elif provirusStart != 1 and clipPartial == provirusActual:
      return returnObj

  elif readNear3p:
    provirusStart = readInfo["start"]
    fragmentLen = len(clip)
    readProviralLen = len(read.seq) - fragmentLen

    proviralEnd = len(proviralSeqs[read.reference_name][0])
    reqProviralEnd = proviralEnd - readProviralLen

    clipPartial = clip[:reqProviralEnd - proviralEnd]
    provirusActual = proviralSeqs[read.reference_name][0][-1 * (reqProviralEnd - proviralEnd):]

    print("HERE {} {} {} {}".format(provirusStart, proviralEnd, readProviralLen, reqProviralEnd))

    if provirusStart == reqProviralEnd:
      return returnObj
    
    elif provirusStart != reqProviralEnd and clipPartial == provirusActual:
      return returnObj

  return None
  

def writeFasta(chimeras, hostClipFastaFn):
  records = []
  for chimera in chimeras:
    
    print(chimera["hostSoftClip"]["clippedFrag"])
    record = SeqRecord(
      id = chimera["read"].qname,
      seq = Seq(chimera["hostSoftClip"]["clippedFrag"]),
      description = ""
    )

    records.append(record)

  SeqIO.write(records, hostClipFastaFn, "fasta")


def alignClipToHost(fafile, hostGenomeIndex, hostClipLen = 17):
  if not os.path.exists(fafile) or os.stat(fafile).st_size == 0:
    printGreen("No records in fasta file. Skipping alignment.") 
    return None

  outputSam = fafile + ".sam"
  command = "bwa mem -T {quality} -k {seed} -a -Y -q {index} {fa} -o {sam}".format(
      index = hostGenomeIndex,
      fa = fafile,
      sam = outputSam,
      quality = hostClipLen,
      seed = hostClipLen - 2)

  child = subprocess.Popen(command, shell = True)
  child.wait()
  if child.poll() != 0:
    raise Exception("Error with alignment")
  
  validIntSites = defaultdict(list)
  qnamesWithMultipleHits = []
  alignment = pysam.AlignmentFile(outputSam, "r")
  for rec in alignment:
    if rec.mapq == 0:
      continue

    if len(validIntSites[rec.qname]) > 0:
      printRed("{}: integration site can't be found due to multiple hits in host genome".format(rec.qname))
      validIntSites.pop(rec.qname)
      qnamesWithMultipleHits.append[rec.qname]
      continue
    elif rec.qname in qnamesWithMultipleHits:
      continue
    
    validIntSites[rec.qname].append(rec)

  return validIntSites


def parseProviralReads(readPairs, proviralSeqs, hostClipFastaFn, clipMinLen = 17):
  validReads = []
  potentialValidChimeras = []

  for rpName in readPairs:
    # must be paired
    if len(readPairs[rpName]) != 2:
      continue
    
    read1 = readPairs[rpName][0]
    read2 = readPairs[rpName][1]
    
    # must contain a valid cell barcode passing allowlist
    if extractCellBarcode(read1) is None:
      continue

    # skip if only single mate mapped
    if read1.is_unmapped or read2.is_unmapped:
      continue
    
    # add to allowed proviral reads...
    validReads.append(read1)
    validReads.append(read2)

    # rearrange depending on where alignment is
    if read1.reference_start > read2.reference_start:
      read1, read2 = read2, read1
    
    # skip if there's multiple soft clips
    if read1.cigarstring.count("S") + read2.cigarstring.count("S") > 1:
      continue

    # move on to chimera analysis
    refLen = len(proviralSeqs[read1.reference_name][0])
    read1AllAlts = getAltAlign(read1)
    read2AllAlts = getAltAlign(read2)

    potentialAltChimera = None
    if read1AllAlts is not None and read2AllAlts is not None:
      read1Alts = [alt for alt in read1AllAlts if alt[0] == read1.reference_name]
      read2Alts = [alt for alt in read2AllAlts if alt[0] == read2.reference_name]
      
      read1AltCheck = None
      read2AltCheck = None
      if len(read1Alts) > 1 or len(read2Alts) > 1:
        print("{}: has multiple alt aligns. Verify manually.".format(read1.qname))
      
      if len(read1Alts) == 1:
        read1AltCheck = checkForPotentialHostClip(read1, refLen, proviralSeqs = proviralSeqs,
          clipMinLen = clipMinLen, useAlts = read1Alts[0])
      if len(read2Alts) == 1:
        read2AltCheck = checkForPotentialHostClip(read2, refLen, proviralSeqs = proviralSeqs,
          clipMinLen = clipMinLen, useAlts = read2Alts[0])

      if read1AltCheck is None and read2AltCheck is not None:
        potentialAltChimera = read2AltCheck
      elif read1AltCheck is not None and read2AltCheck is None:
        potentialAltChimera = read1AltCheck

    potentialChimera = None
    read1Check = checkForPotentialHostClip(read1, refLen, proviralSeqs = proviralSeqs,
      clipMinLen = clipMinLen, useAlts = None)
    read2Check = checkForPotentialHostClip(read2, refLen, proviralSeqs = proviralSeqs,
      clipMinLen = clipMinLen, useAlts = None)  
  
    if read1Check is None and read2Check is not None:
      potentialChimera = read2Check
    elif read1Check is not None and read2Check is None:
      potentialChimera = read1Check

    if potentialAltChimera is not None and potentialChimera is not None:
      printRed("{}: please verify. Clip identified in both alt and normal align.".format(read1.qname))
    elif potentialAltChimera is not None:
      potentialValidChimeras.append(potentialAltChimera)
      printRed(read1.to_string())
      printRed(read2.to_string())
    elif potentialChimera is not None:
      potentialValidChimeras.append(potentialChimera)
      printRed(read1.to_string())
      printRed(read2.to_string())
  writeFasta(potentialValidChimeras, hostClipFastaFn)

  returnVal = {"validReads" : validReads, "potentialValidChimeras": potentialValidChimeras}
  return returnVal


def parseUnmappedReads(readPairs, proviralSeqs, proviralLTRSeqs, LTRClipMinLen = 11, hostClipMinLen = 17, minHostQuality = 30):
  validUnmapped = []
  validChimera = []
  potentialChimera = []
  
  for k in readPairs:
    readPair = readPairs[k]
    if readPair[0].reference_name in proviralSeqs.keys():
      viralRead = readPair[0]
      hostRead = readPair[1]
    else:
      viralRead = readPair[1]
      hostRead = readPair[0]

    # host read must have high enough mapq
    # for viral read, no check since mapq is unrealiable if using multiple viral seqs
    if hostRead.mapq < minHostQuality:
      continue
    
    hostReadSubs = hostRead.cigarstring.count("S")
    viralReadSubs = viralRead.cigarstring.count("S")
    
    # can't have mulutiple soft clips present
    if hostReadSubs + viralReadSubs > 1:
      #print("{}: Multiple soft clips in either/both reads, but still valid".format(hostRead.query_name))
      validUnmapped.append(readPair)
      continue

    # if no soft clips, just save as valid unmapped
    if hostReadSubs == 0 and viralReadSubs == 0:
      validUnmapped.append(readPair)
      continue

    # special case. #TODO add this case.
    if hostReadSubs == 1 and viralReadSubs == 1:
      printRed("Soft clip detected in both host and viral")

    # host read soft clip
    elif hostReadSubs == 1:
      tmp = getSoftClip(hostRead, LTRClipMinLen, 3)
      if tmp is not None:
        print(tmp)
      
      potentialHits = isSoftClipProviral(hostRead, proviralLTRSeqs, LTRClipMinLen, ignoreOrient = True)
      if potentialHits:
        validChimera.append(readPair)
      else:
        validUnmapped.append(readPair)

    # viral read soft clip
    elif viralReadSubs == 1:
      refLen = len(proviralSeqs[viralRead.reference_name][0])
      readAllAlts = getAltAlign(viralRead)

      viralSoftClipAlt = None
      if readAllAlts is not None:
        readAlts = [alt for alt in readAllAlts if alt[0] == viralRead.reference_name]
        if len(readAlts) == 1:
          viralSoftClipAlt = checkForPotentialHostClip(viralRead, refLen, proviralSeqs = proviralSeqs,
            clipMinLen = hostClipMinLen, useAlts = readAlts[0])

      viralSoftClip = checkForPotentialHostClip(viralRead, refLen, proviralSeqs = proviralSeqs,
        clipMinLen = hostClipMinLen, useAlts = None)

      if viralSoftClip is not None:
        print("{}: Valid soft clip detected in virus. Proceed further".format(viralRead.query_name))
        print(viralRead.to_string())
        potentialChimera.append(viralSoftClip)
      elif viralSoftClipAlt is not None:
        print("{}: Valid alternate soft clip detected in virus. Proceed further".format(viralRead.query_name))
        print(viralRead.to_string())
        potentialChimera.append(viralSoftClipAlt)

    else:
      validUnmapped.append(readPair)

  return {"validChimera": validChimera,
    "validUnmapped": validUnmapped,
    "potentialChimera": potentialChimera}


def parseCellrangerBam(bamfile, proviralFastaIds, proviralReads, hostReadsWithPotentialChimera, unmappedPotentialChimera, top_n = -1):
  bam = pysam.AlignmentFile(bamfile, "rb", threads = 20)
  
  readIndex = 0
  for read in bam:
    # ignore if optical/PCR duplicate OR without a mate
    if (read.flag & 1024) or (not read.flag & 1):
      readIndex += 1
      continue
    
    refnameIsProviral = read.reference_name in proviralFastaIds
    # supposed to take mate's ref name or if no mate, the next record in BAM file
    nextRefnameIsProviral = read.next_reference_name in proviralFastaIds
    
    cigarString = read.cigartuples
    # 4 is soft clip
    hasSoftClipAtEnd = cigarString != None and (cigarString[-1][0] == 4 or cigarString[0][0] == 4)
    softClipInitThresh = 9
    softClipIsLongEnough = cigarString != None and (cigarString[-1][1] >= softClipInitThresh or cigarString[0][1] >= softClipInitThresh)
    
    # if read is properly mapped in a pair AND not proviral aligned AND there is soft clipping involved
    if (read.flag & 2) and (not refnameIsProviral) and (hasSoftClipAtEnd and softClipIsLongEnough):
      # move to chimera identification
      hostReadsWithPotentialChimera[read.query_name].append(read)
    
    # if there is a mate AND both are proviral only 
    elif refnameIsProviral and nextRefnameIsProviral:
      # save into proviral
      proviralReads[read.query_name].append(read)

    # read or mate must be mapped AND either read or its mate must be proviral
    elif (not read.flag & 14) and (refnameIsProviral or nextRefnameIsProviral):
      # move to chimera identification
      unmappedPotentialChimera[read.query_name].append(read)
    
    readIndex += 1
    
    if readIndex % 10000000 == 0:
      print("Parsed {} reads".format(str(readIndex)))

    if top_n != -1 and readIndex > top_n:
      return
    
  return bam


def writeBam(fn, templateBam, reads):
  outputBam = pysam.AlignmentFile(fn, "wb", template = templateBam)
  
  if isinstance(reads, list):
    for read in reads:
      outputBam.write(read)
  else:
    for qname in reads:
      for read in reads[qname]:
        outputBam.write(read)


def importProcessedBam(bamfile, returnDict = True):
  bam = pysam.AlignmentFile(bamfile, "rb", threads = 20)

  if returnDict:
    val = defaultdict(list)
  else:
    val = []

  for read in bam:
    if returnDict:
      val[read.query_name].append(read)
    else:
      val.append(read)

  return val


def main(args):
  # output filenames
  outputFNs = {
    "proviralReads": "proviralReads.bam",
    "hostWithPotentialChimera": "hostWithPotentialChimera.bam",
    "umappedWithPotentialChimera": "unmappedWithPotentialChimera.bam",
    "hostWithValidChimera": "hostWithValidChimera.bam",
    "validProviralReads": "validProviralReads.bam",
    "validProviralReadsWithPotentialChimera": "validProviralReadsWithPotentialChimera.bam",
    "viralReadHostClipFasta": "viralReadHostClipFastaFn.fa"
  }

  for k in outputFNs:
    outputFNs[k] = args.outputDir + "/" + outputFNs[k]

  
  # set up initial dictionaries
  dualProviralAlignedReads = defaultdict(list)
  hostReadsWithPotentialChimera = defaultdict(list)
  unmappedPotentialChimera = defaultdict(list)

  #############################
  # Prepare LTR IDs and seqs
  #############################

  # recover all proviral "chromosome" names from partial fasta file used by Cellranger
  printGreen("Getting proviral records")
  proviralSeqs = defaultdict(lambda: [])
  proviralFastaIds = getProviralFastaIDs(args.viralFasta, proviralSeqs)

  # get possible LTR regions from fasta file
  if args.LTRmatches is not None:
    printGreen("Getting potential LTRs")
    potentialLTR = parseLTRMatches(args.LTRmatches, proviralSeqs)
  elif args.LTRpositions is not None:
    printGreen("LTR positions provided as {}".format(args.LTRpositions))
    potentialLTR = parseLTRMatches(args.LTRpositions, proviralSeqs, position = True)


  #############################
  # Parse or load BAM files
  #############################

  if not os.path.exists(outputFNs["proviralReads"]):
    # parse BAM file
    printGreen("Parsing cellranger BAM (namesorted)")
    parseCellrangerBam(bamfile = args.bamfile,
      proviralFastaIds = proviralFastaIds,
      proviralReads = dualProviralAlignedReads,
      hostReadsWithPotentialChimera = hostReadsWithPotentialChimera,
      unmappedPotentialChimera = unmappedPotentialChimera,
      top_n = args.topNReads) #debugging

    # output BAM files
    printGreen("Writing out BAM files of parsed records")

    cellrangerBam = pysam.AlignmentFile(args.bamfile, "rb")
    writeBam(outputFNs["proviralReads"], cellrangerBam, dualProviralAlignedReads)
    writeBam(outputFNs["hostWithPotentialChimera"], cellrangerBam, hostReadsWithPotentialChimera)
    writeBam(outputFNs["umappedWithPotentialChimera"], cellrangerBam, unmappedPotentialChimera)
    cellrangerBam.close()

  else:
    printGreen("Parsed BAM files already found. Importing these files to save time.")
    
    # import files
    dualProviralAlignedReads = importProcessedBam(outputFNs["proviralReads"],
      returnDict = True)
    #hostReadsWithPotentialChimera = importProcessedBam(outputFNs["hostWithPotentialChimera"],
     # returnDict = True)
    unmappedPotentialChimera = importProcessedBam(outputFNs["umappedWithPotentialChimera"],
      returnDict = True)

  #############################
  # Begin downstream proc
  #############################

  # parse host reads with potential chimera
  #printGreen("Finding valid chimeras from host reads")
  #hostValidChimeras = parseHostReadsWithPotentialChimera(hostReadsWithPotentialChimera,
  #  potentialLTR,
  #  clipMinLen = args.LTRClipLen)
  
  printGreen("Finding valid chimeras from proviral reads")
  proviralValidChimeras = parseProviralReads(
    readPairs = dualProviralAlignedReads,
    proviralSeqs = proviralSeqs,
    hostClipFastaFn = outputFNs["viralReadHostClipFasta"],
    clipMinLen = args.hostClipLen)
    
  printCyanOnGrey("Found {} potential valid chimera(s)".format(len(proviralValidChimeras["potentialValidChimeras"])))
  printGreen("Aligning host clips found on viruses to host genome")
  validIntegrationSites = alignClipToHost(fafile=outputFNs["viralReadHostClipFasta"],
    hostGenomeIndex = args.hostGenomeIndex,
    hostClipLen = args.hostClipLen)

  printGreen("Finding valid unmapped reads that might span between integration site")
  procUnmappedReads = parseUnmappedReads(unmappedPotentialChimera,
    proviralSeqs,
    potentialLTR,
    LTRClipMinLen = args.LTRClipLen,
    hostClipMinLen = args.hostClipLen)
  
  printCyanOnGrey("Found {} valid unmapped + {} with a potentially valid integration site".format(len(procUnmappedReads["validUnmapped"]), len(procUnmappedReads["validChimera"])))


  #############################
  # Export proc files
  #############################

  # write out processed files
  printGreen("Writing out processed bam files")
  cellrangerBam = pysam.AlignmentFile(args.bamfile, "rb")
  # writeBam(args.outputDir + "/" + outputFNs["hostWithValidChimera"],
  #   cellrangerBam,
  #   hostValidChimeras["validReads"])

  writeBam(outputFNs["validProviralReads"],
    cellrangerBam,
    proviralValidChimeras["validReads"])

  writeBam(outputFNs["validProviralReadsWithPotentialChimera"],
    cellrangerBam,
    [x["read"] for x in proviralValidChimeras["potentialValidChimeras"]])
  cellrangerBam.close()


if __name__ == '__main__':
  # set up command line arguments
  parser = argparse.ArgumentParser(
    description = "Identify HIV-associated reads amidst cellular scATAC reads")

  parser.add_argument("--bamfile",
    required = True,
    help = "Name sorted Cellranger BAM file")
  parser.add_argument("--outputDir",
    required = True,
    help = "Output bam files")
  parser.add_argument("--viralFasta",
    required = True,
    help = "Viral fasta file (can have multiple sequences in single file)")
  parser.add_argument("--topNReads",
    default = -1,
    type = int,
    help = "Limit to n number of records in BAM file. Default is all (-1)")
  parser.add_argument("--LTRmatches",
    help = "blastn table output format for LTR matches to HXB2 LTR")
  parser.add_argument("--LTRpositions",
    help = "if only using one viral sequence, detail LTR positions (1-index) by 5' start, 5' end, 3' start, 3'end (ex: 1,634,9086,9719)")
  parser.add_argument("--LTRClipLen",
    default = 11,
    type = int,
    help = "Number of bp to extend into LTR from a chimeric fragment")
  parser.add_argument("--hostClipLen",
    default = 17,
    type = int,
    help = "Number of bp to extend into host genome from a chimeric fragment")
  parser.add_argument("--hostGenomeIndex",
    help = "Prefix of bwa indexed host reference genome (NO provirus sequences included)")

  args = parser.parse_args()

  if not os.path.exists(args.outputDir):
    os.makedirs(args.outputDir)

  if not os.path.exists(args.bamfile):
    raise Exception("BAM file not found")

  if not os.path.exists(args.viralFasta):
    raise Exception("viral FASTA file not found")

  if args.LTRmatches is not None and args.LTRpositions is not None:
    raise Exception("LTRmatches and LTRpositions cannot both be set")
  elif args.LTRmatches is None and args.LTRpositions is None:
    raise Exception("One of LTRmatches and LTRpositions must be specified")
  elif args.LTRpositions is not None and len(args.LTRpositions.split(",")) != 4:
    raise Exception("LTRpositions must have LTR positions: 5' start, 5' end, 3' start, 3'end (ex: 1,634,9086,9719)")
  elif args.LTRmatches is not None and not os.path.exists(args.LTRmatches):
    raise Exception("LTRmatches file does not exist")


  main(args)
