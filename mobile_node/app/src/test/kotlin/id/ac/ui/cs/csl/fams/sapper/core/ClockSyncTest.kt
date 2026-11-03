package id.ac.ui.cs.csl.fams.sapper.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ClockSyncTest {

    @Test
    fun medianOfOddCount() {
        assertEquals(5L, ClockSync.median(listOf(9, 1, 5)))
        assertEquals(-3L, ClockSync.median(listOf(-3, 0, -10)))
    }

    @Test
    fun medianOfEvenCountIsTruncatedMeanOfMiddle() {
        assertEquals(3L, ClockSync.median(listOf(1, 2, 4, 9))) // (2+4)/2
        assertEquals(-1L, ClockSync.median(listOf(-4, 0, -2, 1))) // (-2+0)/2
        assertEquals(2L, ClockSync.median(listOf(1, 2, 3, 4))) // (2+3)/2 truncated
    }

    @Test
    fun medianOfEmptyIsNull() {
        assertNull(ClockSync.median(emptyList()))
    }

    @Test
    fun offsetIsMeanOfOneWayDelays() {
        val c = ClockSync()
        // t0=100 phone sends, t1=150 backend receives, t2=160 backend replies,
        // t3=200 phone receives → rtt = 100-10 = 90? (t3-t0)-(t2-t1) = 100-10 = 90;
        // offset = ((150-100)+(160-200))/2 = (50-40)/2 = 5.
        val o = c.processResponse(100, 150, 160, 200)
        assertEquals(5L, o)
        assertEquals(90L, c.lastRttMs)
    }

    @Test
    fun negativeRttSampleIsRejected() {
        val c = ClockSync()
        // The two clocks moved apart between request and reply (NTP step, stale payload):
        // (t3-t0)-(t2-t1) = 100 - 200 < 0. Folding it in would drag the offset badly.
        assertNull(c.processResponse(100, 5000, 5200, 200))
        assertEquals(0L, c.offsetMs)
        assertFalse(c.isSynced)
    }

    @Test
    fun burstOfFiveAppliesMedian() {
        val c = ClockSync()
        val offsets = mutableListOf<Long>()
        // Five rounds with a symmetric 40 ms rtt and server clock 1000 ms ahead,
        // one wildly asymmetric outlier that must not move the median much.
        for (i in 0 until 4) {
            val o = c.processResponse(0L, 1000L + i, 1001L + i, 41L)!!
            offsets.add(o)
        }
        offsets.add(c.processResponse(0L, 9000L, 9000L, 41L)!!) // skewed sample
        c.applyOffsets(offsets)
        assertTrue(c.isSynced)
        assertEquals(5, c.sampleCount)
        assertTrue("offset must stay near 1000, got ${c.offsetMs}", c.offsetMs in 900..5000)
        assertEquals(c.offsetMs, ClockSync.median(offsets.sorted()))
    }

    @Test
    fun correctedNowAddsOffset() {
        val c = ClockSync()
        c.applyOffsets(listOf(250))
        assertEquals(1250L, c.correctedNow(1000))
    }

    @Test
    fun resetClearsEverything() {
        val c = ClockSync()
        c.applyOffsets(listOf(250))
        c.reset()
        assertEquals(0L, c.offsetMs)
        assertFalse(c.isSynced)
        assertEquals(0L, c.lastRttMs)
    }

    @Test
    fun emptyOffsetsLeaveCurrentAlone() {
        val c = ClockSync()
        c.applyOffsets(listOf(7))
        c.applyOffsets(emptyList())
        assertEquals(7L, c.offsetMs)
        assertTrue(c.isSynced)
    }
}
