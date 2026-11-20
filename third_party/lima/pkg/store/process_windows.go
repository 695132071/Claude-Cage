// SPDX-FileCopyrightText: Copyright The Lima Authors
// SPDX-License-Identifier: Apache-2.0

package store

import (
	"time"

	"golang.org/x/sys/windows"
)

// FAT-family file systems round write times down by up to two seconds, which
// would otherwise make a live owner look newer than its own PID file.
const pidFileTimeSlack = 2 * time.Second

// A process created after the PID file was written cannot be its owner. This
// also covers old PID files without a boot marker (Windows has no boot ID).
// Query failures retain the marker: they do not prove that its owner is gone.
func processStartedAfter(pid int, modified time.Time) bool {
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	var created, exited, kernel, user windows.Filetime
	if windows.GetProcessTimes(handle, &created, &exited, &kernel, &user) != nil {
		return false
	}
	return created.Nanoseconds() > modified.Add(pidFileTimeSlack).UnixNano()
}

// processExited reports whether pid names a process that has already exited.
// Windows keeps an exited process openable while any handle to it remains, so
// a successful os.FindProcess does not mean the process is still running. The
// PID cannot be reused while that handle exists, so the object is the recorded
// process. Any failure to tell is reported as still running.
func processExited(pid int) bool {
	handle, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	event, err := windows.WaitForSingleObject(handle, 0)
	return err == nil && event == windows.WAIT_OBJECT_0
}
