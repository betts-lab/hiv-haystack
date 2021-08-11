from scripts.baseFunctions import extractCellBarcode, separateCigarString
from csv import writer

class IntegrationSite(object):
  def __init__(self, chr, orient, pos):
    super().__init__()

    self.chr = chr
    self.orient = orient
    self.pos = pos

  def __str__(self):
    return("int site at {}{}{}".format(self.chr, self.orient, self.pos))

  def returnAsList(self):
    return [self.chr, self.orient, self.pos]


class ProviralFragment(object):
  def __init__(self):
    super().__init__()
    
    self.seqname = ""
    self.startBp = 0 #0-based start pos
    self.endBp = 0 #0-based end pos (endBp is the actual end Bp as opposed to position + 1)
    self.cbc = ""
    self.usingAlt = None
    self.confirmedAlt = False

  def __str__(self):
    return "{} {}:{}-{}".format(self.cbc, self.seqname, self.startBp, self.endBp)

  def setManually(self, seqname, startBp, endBp, cbc, usingAlt = None):
    self.seqname = seqname
    self.startBp = startBp #0-based start pos
    self.endBp = endBp #0-based end pos (endBp is the actual end Bp as opposed to position + 1)
    self.cbc = cbc
    self.usingAlt = usingAlt

  def setFromRead(self, read):
    self.seqname = read.reference_name
    self.startBp = read.reference_start
    self.endBp = read.reference_end - 1
    self.cbc = extractCellBarcode(read)

  def setAlt(self, usingAlt):
    if usingAlt is None:
      self.usingAlt = None
    elif type(usingAlt) == list and len(usingAlt) == 0:
      self.usingAlt = None
    else:
      self.usingAlt = usingAlt

  def confirmedAltCase(self):
    newSeqname, newPos, newCigarstring = self.usingAlt
    # newCigar = separateCigarString(newCigarstring)
    origLen = self.endBp - self.startBp + 1

    self.seqname = newSeqname
    self.newPos = newPos - 1 # to follow 0-index since pysam doesn't auto adjust this, unlike for read.reference_start
    self.endPos = self.newPos + origLen - 1 # following same nomenclature

    self.confirmedAlt = True

  def returnAsList(self):
    return [self.cbc, self.seqname, self.startBp, self.endBp, str(self.usingAlt), str(self.confirmedAlt)]

  
class ChimericRead(object):
  def __init__(self, read, intsite, proviralFragment):
    super().__init__()
    
    self.read = read
    self.intsite = intsite
    self.proviralFragment = proviralFragment

  def __str__(self):
    return "{} is chimeric with {}. Proviral fragment: {}".format(
      self.read.query_name,
      str(self.intsite),
      str(self.proviralFragment))


class ReadPairDualProviral(object):
  def __init__(self, read1 : ProviralFragment, read2 : ProviralFragment):
    super().__init__()
    self.read1 = read1
    self.read2 = read2
    self.potentialEditRead = ""
    self.potentialEditData = None
    self.potentialEditIsAlt = False

  def setPotentialClipEdit(self, readNum, readData, isAlt):
    self.potentialEditRead = readNum
    self.potentialEditData = readData
    self.potentialEditIsAlt = isAlt

  def unsetPotentialClipEdit(self):
    self.potentialEditRead = ""
    self.potentialEditData = None
    self.potentialEditIsAlt = False

  def updateWithConfirmedEdit(self, newProviralFrag):
    if self.potentialEditRead == "read1":
      self.read1 = newProviralFrag
      self.read2.confirmedAltCase()

    elif self.potentialEditRead == "read2":
      self.read2 = newProviralFrag
      self.read1.confirmedAltCase()

  def returnAsList(self):
    read1List = self.read1.returnAsList()
    read2List = self.read2.returnAsList()

    if self.potentialEditIsAlt and self.potentialEditRead == "read1":
      read1List.append([self.potentialEditIsAlt, True])
      read2List.append([self.potentialEditIsAlt, False])


class CompiledDataset(object):
  def __init__(self,
    validChimerasFromHostReads,
    validChimerasFromViralReads,
    validChimerasFromUnmappedReads,
    validViralReads,
    unmappedViralReads):

    super().__init__()
    self.integrationSites = []
    self.pairedViralFrags = []
    # self.singleViralFrags = []
    self.collatedViralFrags = []

    for c in validChimerasFromViralReads:
      self.integrationSites.append(c)
      self.collatedViralFrags.append(c.proviralFragment)

    for x in validChimerasFromHostReads:
      if len(x['minus']) != 0:
        self.integrationSites = self.integrationSites + x['minus']
        # self.collatedViralFrags.append(c.proviralFragment) # this should already be added in the validViralReads

      elif len(x['plus']) != 0:
        self.integrationSites = self.integrationSites + x['plus']
    
    for key in validChimerasFromUnmappedReads:
      alignedSites = validChimerasFromUnmappedReads[key]
      for i in alignedSites:
        self.integrationSites.append(i)

    # parse through paired viral reads
    for v in validViralReads:
      self.pairedViralFrags.append(v)
      
      read1 = v.read1.returnAsList()
      read2 = v.read2.returnAsList()

      self.collatedViralFrags.append(read1)
      self.collatedViralFrags.append(read2)

    # parse through unampped viral reads
    for v in unmappedViralReads:
      self.collatedViralFrags.append(v)


  def exportIntegrationSiteTSV(self, fn):
    output = [[x.proviralFragment.cbc] + x.intsite.returnAsList() for x in self.integrationSite]

    with open(fn, "w") as tsvfile:
      writ = writer(tsvfile, delimiter = "\t", newline = "\n")
      for o in output:
        writ.writerow(o)

  def exportProviralCoverageTSV(self, fn):
    with open(fn, "w") as tsvfile:
      writ = writer(tsvfile, delimiter = "\t", newline = "\n")
      for o in self.collatedViralFrags:
        writ.writerow(o)
