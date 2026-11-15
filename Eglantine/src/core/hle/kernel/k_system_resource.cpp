// SPDX-FileCopyrightText: Copyright 2026 Eden Emulator Project
// SPDX-License-Identifier: GPL-3.0-or-later

// SPDX-FileCopyrightText: Copyright 2022 yuzu Emulator Project
// SPDX-License-Identifier: GPL-2.0-or-later

#include <cstdlib>

#include "common/literals.h"
#include "core/core.h"
#include "core/hle/kernel/k_scoped_resource_reservation.h"
#include "core/hle/kernel/k_system_resource.h"

namespace Kernel {

using namespace Common::Literals;

Result KSecureSystemResource::Initialize(KernelCore& kernel, size_t size, KResourceLimit* resource_limit, KMemoryManager::Pool pool) {
    // Set members.
    m_resource_limit = resource_limit;
    m_resource_size = size;
    m_resource_pool = pool;

    // Determine required size for our secure resource.
    const size_t secure_size = this->CalculateRequiredSecureMemorySize();

    // Reserve memory for our secure resource.
    KScopedResourceReservation memory_reservation(kernel, m_resource_limit, Svc::LimitableResource::PhysicalMemoryMax, secure_size);
    R_UNLESS(memory_reservation.Succeeded(), ResultLimitReached);

    // Allocate secure memory.
    R_TRY(KSystemControl::AllocateSecureMemory(kernel, std::addressof(m_resource_address),
                                               m_resource_size, static_cast<u32>(m_resource_pool)));
    ASSERT(m_resource_address != 0);

    // Ensure we clean up the secure memory, if we fail past this point.
    ON_RESULT_FAILURE {
        KSystemControl::FreeSecureMemory(kernel, m_resource_address, m_resource_size,
                                         static_cast<u32>(m_resource_pool));
    };

    // Check that our allocation is bigger than the reference counts needed for it.
    const size_t rc_size =
        Common::AlignUp(KPageTableSlabHeap::CalculateReferenceCountSize(m_resource_size), PageSize);
    R_UNLESS(m_resource_size > rc_size, ResultOutOfMemory);

    // Get resource pointer.
    KPhysicalAddress resource_paddr =
        KPageTable::GetHeapPhysicalAddress(kernel, m_resource_address);
    auto* resource =
        kernel.System().DeviceMemory().GetPointer<KPageTableManager::RefCount>(resource_paddr);

    // EglantinePS5: the page manager backs its pages with host memory, zeroed and all of it real
    // on the console: Ocarina of Time's title asks for 47 MiB, one large block of the heap, and
    // only the memory blocks and block infos of the kernel's own tables come out of it (the page
    // tables are host tables here). EDEN_KERNEL_RESOURCE_MIB=<N> gives it N MiB (at least 4); the
    // guest still sees the size it asked for. EDEN_KERNEL_RESOURCE in the log says what is used.
    size_t managed_size = m_resource_size - rc_size;
    if (const char* const chosen = std::getenv("EDEN_KERNEL_RESOURCE_MIB")) {
        const size_t asked = static_cast<size_t>(std::strtoull(chosen, nullptr, 10)) << 20;
        if (asked >= (4_MiB) && asked < managed_size) {
            managed_size = asked;
        }
    }

    // Initialize slab heaps.
    m_dynamic_page_manager.Initialize(m_resource_address + rc_size, managed_size, PageSize);
    m_page_table_heap.Initialize(std::addressof(m_dynamic_page_manager), 0, resource);
    m_memory_block_heap.Initialize(std::addressof(m_dynamic_page_manager), 0);
    m_block_info_heap.Initialize(std::addressof(m_dynamic_page_manager), 0);

    // Initialize managers.
    m_page_table_manager.Initialize(std::addressof(m_dynamic_page_manager),
                                    std::addressof(m_page_table_heap));
    m_memory_block_slab_manager.Initialize(std::addressof(m_dynamic_page_manager),
                                           std::addressof(m_memory_block_heap));
    m_block_info_manager.Initialize(std::addressof(m_dynamic_page_manager),
                                    std::addressof(m_block_info_heap));

    // Set our managers.
    this->SetManagers(m_memory_block_slab_manager, m_block_info_manager, m_page_table_manager);

    // Commit the memory reservation.
    memory_reservation.Commit();

    // Open reference to our resource limit.
    m_resource_limit->Open(kernel);

    // Set ourselves as initialized.
    m_is_initialized = true;

    R_SUCCEED();
}

void KSecureSystemResource::Finalize(KernelCore& kernel) {
    // Check that we have no outstanding allocations.
    ASSERT(m_memory_block_slab_manager.GetUsed() == 0);
    ASSERT(m_block_info_manager.GetUsed() == 0);
    ASSERT(m_page_table_manager.GetUsed() == 0);

    // Free our secure memory.
    KSystemControl::FreeSecureMemory(kernel, m_resource_address, m_resource_size,
                                     static_cast<u32>(m_resource_pool));

    // Release the memory reservation.
    m_resource_limit->Release(kernel, Svc::LimitableResource::PhysicalMemoryMax,
                              this->CalculateRequiredSecureMemorySize());

    // Close reference to our resource limit.
    m_resource_limit->Close(kernel);
}

size_t KSecureSystemResource::CalculateRequiredSecureMemorySize(size_t size, KMemoryManager::Pool pool) {
    return KSystemControl::CalculateRequiredSecureMemorySize(size, static_cast<u32>(pool));
}

} // namespace Kernel
